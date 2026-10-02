from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[ContactRole] = mapped_column(
        Enum(ContactRole, name="contact_role"), default=ContactRole.OTHER
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
        Enum(PostStatus, name="post_status"), default=PostStatus.NEW
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    company: Mapped["Company | None"] = relationship(back_populates="posts")
    outreaches: Mapped[list["Outreach"]] = relationship(back_populates="post")


class Outreach(Base):
    __tablename__ = "outreaches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    post_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("posts.id", ondelete="SET NULL")
    )
    channel: Mapped[ChannelType] = mapped_column(
        Enum(ChannelType, name="channel_type")
    )
    state: Mapped[OutreachState] = mapped_column(
        Enum(OutreachState, name="outreach_state"),
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outreach_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("outreaches.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, name="message_direction")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status"),
        default=MessageStatus.DRAFTED,
    )
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status"),
        default=ReviewStatus.PENDING_REVIEW,
        server_default=ReviewStatus.PENDING_REVIEW.value,
        nullable=False,
    )
    edited_text: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    outreach: Mapped["Outreach"] = relationship(back_populates="messages")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class AttentionItem(Base):
    __tablename__ = "attention_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[AttentionType] = mapped_column(
        Enum(AttentionType, name="attention_type"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[AttentionStatus] = mapped_column(
        Enum(AttentionStatus, name="attention_status"),
        default=AttentionStatus.OPEN,
        server_default=AttentionStatus.OPEN.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NotificationLog(Base):
    __tablename__ = "notifications_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
