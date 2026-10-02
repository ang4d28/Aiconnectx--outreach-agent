"""initial migration — companies, contacts, posts, outreaches, messages, settings

Revision ID: 001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _create_enums(bind: sa.connection) -> None:
    """Create PostgreSQL enum types before tables that reference them."""
    contact_role = sa.Enum("recruiter", "founder", "hr", "md", "other", name="contact_role")
    contact_role.create(bind, checkfirst=True)

    post_status = sa.Enum("new", "processed", "skipped", name="post_status")
    post_status.create(bind, checkfirst=True)

    channel_type = sa.Enum("linkedin", "email", name="channel_type")
    channel_type.create(bind, checkfirst=True)

    outreach_state = sa.Enum(
        "new", "qualified", "targeted", "enriched", "drafted", "contacted",
        "replied", "interested", "no_opportunity", "wrong_person",
        "not_now", "unsubscribed", "closed",
        name="outreach_state",
    )
    outreach_state.create(bind, checkfirst=True)

    message_direction = sa.Enum("outbound", "inbound", name="message_direction")
    message_direction.create(bind, checkfirst=True)

    message_status = sa.Enum("drafted", "sent", "failed", name="message_status")
    message_status.create(bind, checkfirst=True)


def _drop_enums(bind: sa.connection) -> None:
    """Drop PostgreSQL enum types in reverse order of creation."""
    for name in [
        "message_status",
        "message_direction",
        "outreach_state",
        "channel_type",
        "post_status",
        "contact_role",
    ]:
        bind.execute(sa.text(f'DROP TYPE IF EXISTS "{name}" CASCADE'))


def upgrade() -> None:
    bind = op.get_bind()
    _create_enums(bind)

    op.create_table(
        "companies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("website", sa.String(255)),
        sa.Column("linkedin_url", sa.String(500)),
        sa.Column("industry", sa.String(100)),
        sa.Column("size", sa.String(50)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), unique=True),
        sa.Column("linkedin_url", sa.String(500)),
        sa.Column("title", sa.String(255)),
        sa.Column("role", sa.Enum(name="contact_role"), nullable=False, server_default="other"),
        sa.Column("is_unsubscribed", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("last_touch_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "posts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("url", sa.String(500), unique=True, nullable=False),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id", ondelete="SET NULL")),
        sa.Column("poster_name", sa.String(255)),
        sa.Column("poster_profile_url", sa.String(500)),
        sa.Column("poster_title", sa.String(255)),
        sa.Column("roles", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("raw_text", sa.Text),
        sa.Column("relevance_score", sa.Float, nullable=False, server_default=sa.text("0.0")),
        sa.Column("status", sa.Enum(name="post_status"), nullable=False, server_default="new"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "outreaches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("contact_id", sa.Integer, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("post_id", sa.Integer, sa.ForeignKey("posts.id", ondelete="SET NULL")),
        sa.Column("channel", sa.Enum(name="channel_type"), nullable=False),
        sa.Column("state", sa.Enum(name="outreach_state"), nullable=False, server_default="new"),
        sa.Column("sent_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("follow_up_due", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("follow_up_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("outreach_id", sa.Integer, sa.ForeignKey("outreaches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction", sa.Enum(name="message_direction"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("sent_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("status", sa.Enum(name="message_status"), nullable=False, server_default="drafted"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("messages")
    op.drop_table("outreaches")
    op.drop_table("posts")
    op.drop_table("contacts")
    op.drop_table("companies")
    op.drop_table("settings")

    _drop_enums(bind)
