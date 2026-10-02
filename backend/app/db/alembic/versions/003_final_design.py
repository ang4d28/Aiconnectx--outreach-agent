"""final design: connect states, message review fields, attention and notification tables

Revision ID: 003_final_design
Revises: 002_message_review
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003_final_design"
down_revision: Union[str, None] = "002_message_review"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

REVIEW_VALUES = ("pending_review", "approved", "cancelled", "edited", "auto_released")
ATTENTION_TYPES = ("interested", "question", "channel_error", "needs_manual")
ATTENTION_STATUSES = ("open", "resolved", "dismissed")
NEW_STATES = ("connect_sent", "connected", "dm_sent")


def _enum(name: str, values: tuple[str, ...]) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def _create_enums() -> None:
    bind = op.get_bind()
    for name, values in (
        ("review_status", REVIEW_VALUES),
        ("attention_type", ATTENTION_TYPES),
        ("attention_status", ATTENTION_STATUSES),
    ):
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in NEW_STATES:
            op.execute(f"ALTER TYPE outreach_state ADD VALUE IF NOT EXISTS '{value}'")

    _create_enums()

    op.add_column("messages", sa.Column("hold_until", sa.DateTime(timezone=True)))
    op.add_column(
        "messages",
        sa.Column(
            "review_status",
            _enum("review_status", REVIEW_VALUES),
            nullable=False,
            server_default="pending_review",
        ),
    )
    op.add_column("messages", sa.Column("edited_text", sa.Text))
    op.add_column("messages", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.add_column("messages", sa.Column("whatsapp_message_id", sa.String(255)))

    op.create_table(
        "attention_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", _enum("attention_type", ATTENTION_TYPES), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "status",
            _enum("attention_status", ATTENTION_STATUSES),
            nullable=False,
            server_default="open",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "notifications_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("notifications_log")
    op.drop_table("attention_items")
    for column in ("whatsapp_message_id", "reviewed_at", "edited_text", "review_status", "hold_until"):
        op.drop_column("messages", column)
    for name in ("attention_status", "attention_type", "review_status"):
        op.execute(f'DROP TYPE IF EXISTS "{name}"')
