"""add approved and rejected to message_status

Revision ID: 002_message_review
Revises: 001_initial
Create Date: 2026-10-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "002_message_review"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE message_status ADD VALUE IF NOT EXISTS 'approved'")
        op.execute("ALTER TYPE message_status ADD VALUE IF NOT EXISTS 'rejected'")


def downgrade() -> None:
    pass
