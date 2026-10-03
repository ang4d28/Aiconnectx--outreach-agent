"""users table for multi-user dashboard auth, plus reviewer audit columns

Revision ID: 006_users
Revises: 005_inquiries
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_users"
down_revision: Union[str, None] = "005_inquiries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_subject", sa.String(255)),
        sa.Column("role", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider", "provider_subject", name="uq_users_provider_subject"),
        sa.UniqueConstraint("provider", "email", name="uq_users_provider_email"),
    )
    op.add_column(
        "messages",
        sa.Column(
            "reviewed_by",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_messages_reviewed_by_users"),
        ),
    )
    op.add_column(
        "attention_items",
        sa.Column(
            "resolved_by",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_attention_items_resolved_by_users"),
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_attention_items_resolved_by_users", "attention_items", type_="foreignkey")
    op.drop_column("attention_items", "resolved_by")
    op.drop_constraint("fk_messages_reviewed_by_users", "messages", type_="foreignkey")
    op.drop_column("messages", "reviewed_by")
    op.drop_table("users")
