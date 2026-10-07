"""store the email subject with each draft

Revision ID: 011_message_subject
Revises: 010_perf_indexes
Create Date: 2026-10-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011_message_subject"
down_revision: Union[str, None] = "010_perf_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("subject", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "subject")
