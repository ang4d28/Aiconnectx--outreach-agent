"""inquiries: status and notes for the dashboard

Revision ID: 008_inquiry_status
Revises: 007_created_at_not_null
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_inquiry_status"
down_revision: Union[str, None] = "007_created_at_not_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inquiries", sa.Column("status", sa.String(20), nullable=False, server_default="new"))
    op.add_column("inquiries", sa.Column("notes", sa.Text(), nullable=True))
    op.create_index("ix_inquiries_status", "inquiries", ["status"])


def downgrade() -> None:
    op.drop_index("ix_inquiries_status", table_name="inquiries")
    op.drop_column("inquiries", "notes")
    op.drop_column("inquiries", "status")
