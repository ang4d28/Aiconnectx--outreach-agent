"""indexes for dashboard filters, sorting and joins

Revision ID: 010_perf_indexes
Revises: 009_message_blocked
Create Date: 2026-10-05 00:00:00.000000

Postgres does not index foreign keys on its own. These cover the columns the dashboard and the workers
filter, join and sort by (state, channel, status, direction, created_at, sent_at, company/contact/outreach ids).
The same indexes are declared on the models, so create_all and alembic agree.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "010_perf_indexes"
down_revision: Union[str, None] = "009_message_blocked"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_users_role", "users", ["role"], if_not_exists=True)
    op.create_index("ix_contacts_company_id", "contacts", ["company_id"], if_not_exists=True)
    op.create_index("ix_posts_status", "posts", ["status"], if_not_exists=True)
    op.create_index("ix_posts_company_id", "posts", ["company_id"], if_not_exists=True)
    op.create_index("ix_posts_created_at", "posts", ["created_at"], if_not_exists=True)
    op.create_index("ix_outreaches_state", "outreaches", ["state"], if_not_exists=True)
    op.create_index("ix_outreaches_channel_state", "outreaches", ["channel", "state"], if_not_exists=True)
    op.create_index("ix_outreaches_contact_id", "outreaches", ["contact_id"], if_not_exists=True)
    op.create_index("ix_outreaches_post_id", "outreaches", ["post_id"], if_not_exists=True)
    op.create_index("ix_outreaches_created_at", "outreaches", ["created_at"], if_not_exists=True)
    op.create_index("ix_outreaches_follow_up_due", "outreaches", ["follow_up_due"], if_not_exists=True)
    op.create_index("ix_messages_outreach_direction", "messages", ["outreach_id", "direction"], if_not_exists=True)
    op.create_index("ix_messages_direction_status", "messages", ["direction", "status"], if_not_exists=True)
    op.create_index("ix_messages_sent_at", "messages", ["sent_at"], if_not_exists=True)
    op.create_index("ix_attention_items_status_type", "attention_items", ["status", "type"], if_not_exists=True)
    op.create_index("ix_attention_items_created_at", "attention_items", ["created_at"], if_not_exists=True)
    op.create_index("ix_notifications_log_type_sent_at", "notifications_log", ["type", "sent_at"], if_not_exists=True)
    op.create_index("ix_inquiries_status_created_at", "inquiries", ["status", "created_at"], if_not_exists=True)
    op.create_index("ix_inquiries_created_at", "inquiries", ["created_at"], if_not_exists=True)
    op.execute("ANALYZE")


def downgrade() -> None:
    op.drop_index("ix_users_role", table_name="users", if_exists=True)
    op.drop_index("ix_contacts_company_id", table_name="contacts", if_exists=True)
    op.drop_index("ix_posts_status", table_name="posts", if_exists=True)
    op.drop_index("ix_posts_company_id", table_name="posts", if_exists=True)
    op.drop_index("ix_posts_created_at", table_name="posts", if_exists=True)
    op.drop_index("ix_outreaches_state", table_name="outreaches", if_exists=True)
    op.drop_index("ix_outreaches_channel_state", table_name="outreaches", if_exists=True)
    op.drop_index("ix_outreaches_contact_id", table_name="outreaches", if_exists=True)
    op.drop_index("ix_outreaches_post_id", table_name="outreaches", if_exists=True)
    op.drop_index("ix_outreaches_created_at", table_name="outreaches", if_exists=True)
    op.drop_index("ix_outreaches_follow_up_due", table_name="outreaches", if_exists=True)
    op.drop_index("ix_messages_outreach_direction", table_name="messages", if_exists=True)
    op.drop_index("ix_messages_direction_status", table_name="messages", if_exists=True)
    op.drop_index("ix_messages_sent_at", table_name="messages", if_exists=True)
    op.drop_index("ix_attention_items_status_type", table_name="attention_items", if_exists=True)
    op.drop_index("ix_attention_items_created_at", table_name="attention_items", if_exists=True)
    op.drop_index("ix_notifications_log_type_sent_at", table_name="notifications_log", if_exists=True)
    op.drop_index("ix_inquiries_status_created_at", table_name="inquiries", if_exists=True)
    op.drop_index("ix_inquiries_created_at", table_name="inquiries", if_exists=True)
