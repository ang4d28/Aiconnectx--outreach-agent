"""blocked message status (send allowlist)

Revision ID: 009_message_blocked
Revises: 008_inquiry_status
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "009_message_blocked"
down_revision: Union[str, None] = "008_inquiry_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE message_status ADD VALUE IF NOT EXISTS 'blocked'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value; move rows off it instead.
    op.execute("UPDATE messages SET status = 'failed' WHERE status = 'blocked'")
