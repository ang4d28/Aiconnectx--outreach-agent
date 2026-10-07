"""draft versions and regenerated_at

Revision ID: 012_draft_versions
Revises: 011_message_subject
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012_draft_versions"
down_revision: Union[str, None] = "011_message_subject"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("regenerated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "draft_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_draft_versions_draft_id", "draft_versions", ["draft_id"])


def downgrade() -> None:
    op.drop_index("ix_draft_versions_draft_id", table_name="draft_versions")
    op.drop_table("draft_versions")
    op.drop_column("messages", "regenerated_at")
