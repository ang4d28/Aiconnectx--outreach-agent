"""future_pool outreach state

Revision ID: 004_future_pool
Revises: 003_final_design
Create Date: 2026-10-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "004_future_pool"
down_revision: Union[str, None] = "003_final_design"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE outreach_state ADD VALUE IF NOT EXISTS 'future_pool'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value; move rows off it instead.
    op.execute("UPDATE outreaches SET state = 'not_now' WHERE state = 'future_pool'")
