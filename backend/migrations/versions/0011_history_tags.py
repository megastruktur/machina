"""Global tag catalog + mandatory job/conversation classification (W1).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30

Schema only — the tag store, agent tools, HTTP projections and UI arrive in
later slices of the history-tags feature (plans/history-tags.md).

- ``tags`` — the global, server-agnostic tag catalog. ``name`` is the display
  name, stored EXACTLY in normalized form (trimmed, internal whitespace runs
  collapsed to one space, no control characters — the CHECKs reject anything
  else, so callers never store a name that differs from its canonical form);
  canonical uniqueness (case-insensitive, whitespace-collapsed) is enforced by
  the unique functional index ``ux_tags_name_canonical`` on ``lower(name)``.
  Bounds: name 1..64 chars, description <= 500 chars (NULL allowed).
- ``job_tags`` / ``conversation_tags`` — unlimited independent associations
  (no implicit inheritance between the two domains). ``(entity_id, tag_id)``
  primary keys with reverse ``(tag_id, entity_id)`` indexes (``ix_*_tag_id``)
  for tag-led listing/search. ``assignment_source`` is the frozen writer
  vocabulary ``agent | user | system``; ``assigned_by`` is a bounded free-form
  actor reference (empty for the migration backfill).
- Cascades are association-only: deleting a job run / conversation removes its
  association rows and never touches the catalog; the ``tag_id`` FKs are
  RESTRICT, so a used tag cannot be deleted out-of-band (the store/API layer
  implements the later "refuse used-tag deletion unless explicitly forced"
  policy by removing associations first — every entity must keep at least one
  tag, so a forced deletion can only ever release fully-unused tags).
- ``unclassified`` is a reserved catalog row (fixed ``tag_id`` —
  ``RESERVED_UNCLASSIFIED_TAG_ID``), seeded below and backfilled onto every
  existing job run and conversation. A BEFORE DELETE trigger refuses to
  delete it for as long as the schema exists.
- Mandatory-at-least-one-tag invariant, enforced against out-of-band SQL with
  DEFERRABLE INITIALLY DEFERRED constraint triggers so a NEW entity plus its
  first association commit inside one transaction:
  - ``trg_job_runs_mandatory_tag`` / ``trg_conversations_mandatory_tag``
    (AFTER INSERT on the entity, checked at commit): a committed entity must
    have at least one tag.
  - ``trg_job_tags_mandatory_tag`` / ``trg_conversation_tags_mandatory_tag``
    (AFTER DELETE OR UPDATE on the association, checked at commit): an
    association may only go away when the entity keeps another tag or when the
    entity row itself is gone (the association-only cascade delete above is
    therefore exempt). TRUNCATE bypasses row triggers in PostgreSQL exactly as
    it bypasses foreign keys — out-of-band destructive maintenance is outside
    the invariant's scope, same as for the rest of this chain.
  The invariant lives and dies with the schema: the downgrade below removes
  the enforcement together with the tables it protects. Nothing is weakened
  while the schema exists; after a re-upgrade the triggers are rebuilt and the
  backfill re-assigns ``unclassified`` to any entity created meanwhile.

Chain position: eleventh revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0010``.
First triggers in this chain — owned by the history-tags feature slice.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None

# Fixed identity of the reserved ``unclassified`` catalog row. A constant (not
# a random UUID) keeps re-upgrades idempotent and lets later slices reference
# the default tag without a name lookup. Runtime single source of truth is
# ``mai.tags.RESERVED_UNCLASSIFIED_TAG_ID``; this module mirrors the same
# import-standalone value (the chain must not import application code), and
# tests/test_history_tag_schema.py asserts the mirror equality.
RESERVED_UNCLASSIFIED_TAG_ID = "6a1e9d3c-5b4f-4c2a-9e8d-7f0a1b2c3d4e"
RESERVED_UNCLASSIFIED_NAME = "unclassified"
RESERVED_UNCLASSIFIED_DESCRIPTION = (
    "Reserved default classification assigned to every job and conversation "
    "at creation; replace it with a meaningful tag at the first durable "
    "bookkeeping checkpoint."
)

# Frozen writer vocabulary for association rows.
_SOURCES_SQL = "('agent', 'user', 'system')"

# Backfill + seed run before the triggers exist; ON CONFLICT keeps both
# statements idempotent against any re-execution.
_SEED_TAG_SQL = f"""
INSERT INTO public.tags (tag_id, name, description)
VALUES ('{RESERVED_UNCLASSIFIED_TAG_ID}', '{RESERVED_UNCLASSIFIED_NAME}',
        '{RESERVED_UNCLASSIFIED_DESCRIPTION}')
ON CONFLICT (tag_id) DO NOTHING
"""

_BACKFILL_JOB_TAGS_SQL = f"""
INSERT INTO public.job_tags (job_id, tag_id, assigned_by, assignment_source)
SELECT run_id, '{RESERVED_UNCLASSIFIED_TAG_ID}', '', 'system'
FROM public.job_runs
ON CONFLICT DO NOTHING
"""

_BACKFILL_CONVERSATION_TAGS_SQL = f"""
INSERT INTO public.conversation_tags
    (conversation_id, tag_id, assigned_by, assignment_source)
SELECT conversation_id, '{RESERVED_UNCLASSIFIED_TAG_ID}', '', 'system'
FROM public.conversations
ON CONFLICT DO NOTHING
"""

# Constraint-trigger functions. Each mandatory-tag check exempts entities
# whose row is already gone so the association-only cascade delete can never
# trip the invariant.

_FN_JOB_RUN_HAS_TAG = """
CREATE OR REPLACE FUNCTION public.mai_history_tags_job_run_has_tag()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.job_tags WHERE job_id = NEW.run_id
    ) THEN
        RAISE EXCEPTION 'job run % must keep at least one tag', NEW.run_id;
    END IF;
    RETURN NEW;
END;
$fn$
"""

_FN_JOB_TAG_LEFT = """
CREATE OR REPLACE FUNCTION public.mai_history_tags_job_tag_left()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.job_runs WHERE run_id = OLD.job_id
    ) AND NOT EXISTS (
        SELECT 1 FROM public.job_tags WHERE job_id = OLD.job_id
    ) THEN
        RAISE EXCEPTION 'job run % must keep at least one tag', OLD.job_id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$
"""

_FN_CONVERSATION_HAS_TAG = """
CREATE OR REPLACE FUNCTION public.mai_history_tags_conversation_has_tag()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.conversation_tags
        WHERE conversation_id = NEW.conversation_id
    ) THEN
        RAISE EXCEPTION 'conversation % must keep at least one tag',
            NEW.conversation_id;
    END IF;
    RETURN NEW;
END;
$fn$
"""

_FN_CONVERSATION_TAG_LEFT = """
CREATE OR REPLACE FUNCTION public.mai_history_tags_conversation_tag_left()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.conversations
        WHERE conversation_id = OLD.conversation_id
    ) AND NOT EXISTS (
        SELECT 1 FROM public.conversation_tags
        WHERE conversation_id = OLD.conversation_id
    ) THEN
        RAISE EXCEPTION 'conversation % must keep at least one tag',
            OLD.conversation_id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$
"""

_FN_PROTECT_RESERVED = f"""
CREATE OR REPLACE FUNCTION public.mai_history_tags_protect_reserved_tag()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.tag_id = '{RESERVED_UNCLASSIFIED_TAG_ID}' THEN
        RAISE EXCEPTION 'the reserved unclassified tag cannot be deleted';
    END IF;
    RETURN OLD;
END;
$fn$
"""

_TRIGGERS_SQL = (
    # (constraint?, trigger name, target table, trigger function, DDL body)
    (
        True,
        "trg_job_runs_mandatory_tag",
        "public.job_runs",
        "mai_history_tags_job_run_has_tag",
        _FN_JOB_RUN_HAS_TAG,
    ),
    (
        True,
        "trg_job_tags_mandatory_tag",
        "public.job_tags",
        "mai_history_tags_job_tag_left",
        _FN_JOB_TAG_LEFT,
    ),
    (
        True,
        "trg_conversations_mandatory_tag",
        "public.conversations",
        "mai_history_tags_conversation_has_tag",
        _FN_CONVERSATION_HAS_TAG,
    ),
    (
        True,
        "trg_conversation_tags_mandatory_tag",
        "public.conversation_tags",
        "mai_history_tags_conversation_tag_left",
        _FN_CONVERSATION_TAG_LEFT,
    ),
    (
        False,
        "trg_tags_protect_reserved",
        "public.tags",
        "mai_history_tags_protect_reserved_tag",
        _FN_PROTECT_RESERVED,
    ),
)


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column(
            "tag_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque tag identity, minted by the store (no server "
            "default); the reserved unclassified row carries the fixed "
            "RESERVED_UNCLASSIFIED_TAG_ID.",
        ),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
            comment="Display name in normalized form: trimmed, internal "
            "whitespace runs collapsed to single spaces, single-line "
            "printable (1..64 chars); canonical uniqueness via "
            "ux_tags_name_canonical on lower(name).",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Optional bounded description (<= 500 chars).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Creation time (C01 rule 5: UTC).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last catalog-update time (C01 rule 5: UTC).",
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 64", name="ck_tags_name_len"
        ),
        sa.CheckConstraint(
            "name = btrim(regexp_replace(name, '\\s+', ' ', 'g'))",
            name="ck_tags_name_normalized",
        ),
        sa.CheckConstraint(
            r"name !~ '[\x0000-\x001F\x007F\x0080-\x009F]'",
            name="ck_tags_name_printable",
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 500",
            name="ck_tags_description_len",
        ),
    )
    op.create_index(
        "ux_tags_name_canonical",
        "tags",
        [sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "job_tags",
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Tagged job run; ON DELETE CASCADE — association rows "
            "never outlive their run (association-only cascade).",
        ),
        sa.Column(
            "tag_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Assigned catalog tag; ON DELETE RESTRICT — a used tag "
            "cannot be deleted out-of-band (store/API force policy arrives "
            "in a later slice).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Assignment time (C01 rule 5: UTC).",
        ),
        sa.Column(
            "assigned_by",
            sa.Text(),
            server_default="",
            nullable=False,
            comment="Bounded actor reference (<= 200 chars); empty for the "
            "migration backfill.",
        ),
        sa.Column(
            "assignment_source",
            sa.Text(),
            server_default="system",
            nullable=False,
            comment="Writer vocabulary: agent | user | system.",
        ),
        sa.PrimaryKeyConstraint("job_id", "tag_id"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job_runs.run_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.tag_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            f"assignment_source IN {_SOURCES_SQL}",
            name="ck_job_tags_assignment_source",
        ),
        sa.CheckConstraint(
            "char_length(assigned_by) <= 200", name="ck_job_tags_assigned_by_len"
        ),
    )
    op.create_index(
        "ix_job_tags_tag_id", "job_tags", ["tag_id", "job_id"]
    )

    op.create_table(
        "conversation_tags",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Tagged conversation; ON DELETE CASCADE — association "
            "rows never outlive their conversation (association-only "
            "cascade).",
        ),
        sa.Column(
            "tag_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Assigned catalog tag; ON DELETE RESTRICT — a used tag "
            "cannot be deleted out-of-band (store/API force policy arrives "
            "in a later slice).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Assignment time (C01 rule 5: UTC).",
        ),
        sa.Column(
            "assigned_by",
            sa.Text(),
            server_default="",
            nullable=False,
            comment="Bounded actor reference (<= 200 chars); empty for the "
            "migration backfill.",
        ),
        sa.Column(
            "assignment_source",
            sa.Text(),
            server_default="system",
            nullable=False,
            comment="Writer vocabulary: agent | user | system.",
        ),
        sa.PrimaryKeyConstraint("conversation_id", "tag_id"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.tag_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            f"assignment_source IN {_SOURCES_SQL}",
            name="ck_conversation_tags_assignment_source",
        ),
        sa.CheckConstraint(
            "char_length(assigned_by) <= 200",
            name="ck_conversation_tags_assigned_by_len",
        ),
    )
    op.create_index(
        "ix_conversation_tags_tag_id",
        "conversation_tags",
        ["tag_id", "conversation_id"],
    )

    op.execute(_SEED_TAG_SQL)
    op.execute(_BACKFILL_JOB_TAGS_SQL)
    op.execute(_BACKFILL_CONVERSATION_TAGS_SQL)

    for deferred, name, table, function, create_fn in _TRIGGERS_SQL:
        op.execute(create_fn)
        if deferred:
            # The mandatory-tag checks defer to COMMIT so a NEW entity and
            # its first association can share one transaction.
            event = (
                "AFTER INSERT"
                if table.endswith(("job_runs", "conversations"))
                else "AFTER DELETE OR UPDATE"
            )
            op.execute(
                f"CREATE CONSTRAINT TRIGGER {name}\n"
                f"{event} ON {table}\n"
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW\n"
                f"EXECUTE FUNCTION public.{function}()"
            )
        else:
            # Immediate BEFORE DELETE guard: the reserved row must never
            # leave the catalog.
            op.execute(
                f"CREATE TRIGGER {name}\nBEFORE DELETE ON {table}\n"
                f"FOR EACH ROW EXECUTE FUNCTION public.{function}()"
            )


def downgrade() -> None:
    # Reverse order: triggers + functions first, then tables child-first.
    # Dropping the schema removes the invariant enforcement with it; nothing
    # is left half-enforced (no dangling functions, triggers or tables).
    for _deferred, name, table, _function, _create_fn in _TRIGGERS_SQL:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for _deferred, _name, _table, function, _create_fn in _TRIGGERS_SQL:
        op.execute(f"DROP FUNCTION IF EXISTS public.{function}()")

    op.drop_index("ix_conversation_tags_tag_id", table_name="conversation_tags")
    op.drop_table("conversation_tags")
    op.drop_index("ix_job_tags_tag_id", table_name="job_tags")
    op.drop_table("job_tags")
    op.drop_index("ux_tags_name_canonical", table_name="tags")
    op.drop_table("tags")
