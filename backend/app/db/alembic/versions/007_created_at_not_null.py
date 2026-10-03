"""make created_at / sent_at NOT NULL where the models require it

Revision ID: 007_created_at_not_null
Revises: 006_users
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_created_at_not_null"
down_revision: Union[str, None] = "006_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("attention_items", "created_at"),
    ("inquiries", "created_at"),
    ("notifications_log", "sent_at"),
    ("users", "created_at"),
)


def upgrade() -> None:
    for table, column in COLUMNS:
        op.execute(sa.text(f"UPDATE {table} SET {column} = now() WHERE {column} IS NULL"))
        op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=False)


def downgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=True)
