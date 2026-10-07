from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ContactRole(str, enum.Enum):
    RECRUITER = "recruiter"
    FOUNDER = "founder"
    HR = "hr"
    MD = "md"
    OTHER = "other"


class OutreachState(str, enum.Enum):
    NEW = "new"
    QUALIFIED = "qualified"
    TARGETED = "targeted"
    ENRICHED = "enriched"
    DRAFTED = "drafted"
    CONNECT_SENT = "connect_sent"
    CONNECTED = "connected"
    DM_SENT = "dm_sent"
    CONTACTED = "contacted"
    REPLIED = "replied"
    INTERESTED = "interested"
    NO_OPPORTUNITY = "no_opportunity"
    WRONG_PERSON = "wrong_person"
    NOT_NOW = "not_now"
    FUTURE_POOL = "future_pool"
    UNSUBSCRIBED = "unsubscribed"
    CLOSED = "closed"


class ChannelType(str, enum.Enum):
    LINKEDIN = "linkedin"
    EMAIL = "email"


class MessageDirection(str, enum.Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class MessageStatus(str, enum.Enum):
    DRAFTED = "drafted"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"  # stopped by the send allowlist (sandbox), never sent


class ReviewStatus(str, enum.Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    EDITED = "edited"
    AUTO_RELEASED = "auto_released"


class AttentionType(str, enum.Enum):
    INTERESTED = "interested"
    QUESTION = "question"
    CHANNEL_ERROR = "channel_error"
    NEEDS_MANUAL = "needs_manual"


class AttentionStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class PostStatus(str, enum.Enum):
    NEW = "new"
    PROCESSED = "processed"
    SKIPPED = "skipped"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"
    PENDING = "pending"
    DISABLED = "disabled"


class AuthProvider(str, enum.Enum):
    PASSWORD = "password"
    GOOGLE = "google"
    MICROSOFT = "microsoft"


def _pg_enum(enum_class: type[enum.Enum], name: str) -> Enum:
    """Native Postgres enum that stores the lowercase values, as the migrations create them."""
    return Enum(enum_class, name=name, values_callable=lambda members: [member.value for member in members])


def _lowercase_enum(enum_class: type[enum.Enum], length: int = 20) -> Enum:
    """VARCHAR column holding the lowercase values (no native Postgres enum type to migrate)."""
    return Enum(
        enum_class,
        native_enum=False,
        length=length,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


class User(Base):
    """A dashboard account. Accounts are never linked across providers, not even by email."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("provider", "provider_subject", name="uq_users_provider_subject"),
        UniqueConstraint("provider", "email", name="uq_users_provider_email"),
        Index("ix_users_role", "role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[AuthProvider] = mapped_column(_lowercase_enum(AuthProvider), nullable=False)
    provider_subject: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        _lowercase_enum(UserRole),
        default=UserRole.PENDING,
        server_default=UserRole.PENDING.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    website: Mapped[str | None] = mapped_column(String(255))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    industry: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    contacts: Mapped[list["Contact"]] = relationship(back_populates="company")
    posts: Mapped[list["Post"]] = relationship(back_populates="company")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_company_id", "company_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[ContactRole] = mapped_column(
        _pg_enum(ContactRole, "contact_role"), default=ContactRole.OTHER
    )
    is_unsubscribed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    company: Mapped["Company"] = relationship(back_populates="contacts")
    outreaches: Mapped[list["Outreach"]] = relationship(back_populates="contact")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        Index("ix_posts_status", "status"),
        Index("ix_posts_company_id", "company_id"),
        Index("ix_posts_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    company_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="SET NULL")
    )
    poster_name: Mapped[str | None] = mapped_column(String(255))
    poster_profile_url: Mapped[str | None] = mapped_column(String(500))
    poster_title: Mapped[str | None] = mapped_column(String(255))
    roles: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    raw_text: Mapped[str | None] = mapped_column(Text)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[PostStatus] = mapped_column(
        _pg_enum(PostStatus, "post_status"), default=PostStatus.NEW
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    company: Mapped["Company | None"] = relationship(back_populates="posts")
    outreaches: Mapped[list["Outreach"]] = relationship(back_populates="post")


class Outreach(Base):
    __tablename__ = "outreaches"
    __table_args__ = (
        Index("ix_outreaches_state", "state"),
        Index("ix_outreaches_channel_state", "channel", "state"),
        Index("ix_outreaches_contact_id", "contact_id"),
        Index("ix_outreaches_post_id", "post_id"),
        Index("ix_outreaches_created_at", "created_at"),
        Index("ix_outreaches_follow_up_due", "follow_up_due"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    post_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("posts.id", ondelete="SET NULL")
    )
    channel: Mapped[ChannelType] = mapped_column(
        _pg_enum(ChannelType, "channel_type")
    )
    state: Mapped[OutreachState] = mapped_column(
        _pg_enum(OutreachState, "outreach_state"),
        default=OutreachState.NEW,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    contact: Mapped["Contact"] = relationship(back_populates="outreaches")
    post: Mapped["Post | None"] = relationship(back_populates="outreaches")
    messages: Mapped[list["Message"]] = relationship(back_populates="outreach")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_outreach_direction", "outreach_id", "direction"),
        Index("ix_messages_direction_status", "direction", "status"),
        Index("ix_messages_sent_at", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outreach_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("outreaches.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[MessageDirection] = mapped_column(
        _pg_enum(MessageDirection, "message_direction")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[MessageStatus] = mapped_column(
        _pg_enum(MessageStatus, "message_status"),
        default=MessageStatus.DRAFTED,
    )
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_status: Mapped[ReviewStatus] = mapped_column(
        _pg_enum(ReviewStatus, "review_status"),
        default=ReviewStatus.PENDING_REVIEW,
        server_default=ReviewStatus.PENDING_REVIEW.value,
        nullable=False,
    )
    edited_text: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(200))  # email subject generated with the draft
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(255))
    # Dashboard user who last approved, cancelled or edited it (empty for WhatsApp and automatic actions).
    reviewed_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    regenerated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    outreach: Mapped["Outreach"] = relationship(back_populates="messages")


class DraftVersion(Base):
    """Earlier text of a draft, kept when it is regenerated or a version is restored."""

    __tablename__ = "draft_versions"
    __table_args__ = (Index("ix_draft_versions_draft_id", "draft_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class AttentionItem(Base):
    __tablename__ = "attention_items"
    __table_args__ = (
        Index("ix_attention_items_status_type", "status", "type"),
        Index("ix_attention_items_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[AttentionType] = mapped_column(
        _pg_enum(AttentionType, "attention_type"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[AttentionStatus] = mapped_column(
        _pg_enum(AttentionStatus, "attention_status"),
        default=AttentionStatus.OPEN,
        server_default=AttentionStatus.OPEN.value,
        nullable=False,
    )
    resolved_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NotificationLog(Base):
    __tablename__ = "notifications_log"
    __table_args__ = (
        Index("ix_notifications_log_type_sent_at", "type", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Inquiry(Base):
    """Contact-form submission from the public landing page."""

    __tablename__ = "inquiries"
    __table_args__ = (
        Index("ix_inquiries_status_created_at", "status", "created_at"),
        Index("ix_inquiries_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    company: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    interest: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new", server_default="new")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
