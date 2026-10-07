from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.db.models import (
    ChannelType,
    ContactRole,
    MessageDirection,
    MessageStatus,
    OutreachState,
    PostStatus,
)


# ─── Company ───────────────────────────────────────────────


class CompanyBase(BaseModel):
    name: str = Field(..., max_length=255)
    website: Optional[str] = Field(None, max_length=255)
    linkedin_url: Optional[str] = Field(None, max_length=500)
    industry: Optional[str] = Field(None, max_length=100)
    size: Optional[str] = Field(None, max_length=50)


class CompanyCreate(CompanyBase):
    pass


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    industry: Optional[str] = None
    size: Optional[str] = None


class CompanyRead(CompanyBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─── Contact (Person) ──────────────────────────────────────


class ContactBase(BaseModel):
    company_id: int
    name: str = Field(..., max_length=255)
    email: Optional[str] = Field(None, max_length=255)
    linkedin_url: Optional[str] = Field(None, max_length=500)
    title: Optional[str] = Field(None, max_length=255)
    role: ContactRole = ContactRole.OTHER
    is_unsubscribed: bool = False
    last_touch_at: Optional[datetime] = None


class ContactCreate(ContactBase):
    pass


class ContactUpdate(BaseModel):
    company_id: Optional[int] = None
    name: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    title: Optional[str] = None
    role: Optional[ContactRole] = None
    is_unsubscribed: Optional[bool] = None
    last_touch_at: Optional[datetime] = None


class ContactRead(ContactBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─── Post ──────────────────────────────────────────────────


class PostBase(BaseModel):
    url: str = Field(..., max_length=500)
    company_id: Optional[int] = None
    poster_name: Optional[str] = Field(None, max_length=255)
    poster_profile_url: Optional[str] = Field(None, max_length=500)
    poster_title: Optional[str] = Field(None, max_length=255)
    roles: list[str] = Field(default_factory=list)
    raw_text: Optional[str] = None
    relevance_score: float = 0.0
    status: PostStatus = PostStatus.NEW


class PostCreate(PostBase):
    pass


class PostUpdate(BaseModel):
    url: Optional[str] = None
    company_id: Optional[int] = None
    poster_name: Optional[str] = None
    poster_profile_url: Optional[str] = None
    poster_title: Optional[str] = None
    roles: Optional[list[str]] = None
    raw_text: Optional[str] = None
    relevance_score: Optional[float] = None
    status: Optional[PostStatus] = None


class PostRead(PostBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Outreach ──────────────────────────────────────────────


class OutreachBase(BaseModel):
    contact_id: int
    post_id: Optional[int] = None
    channel: ChannelType
    state: OutreachState = OutreachState.NEW
    sent_at: Optional[datetime] = None
    follow_up_due: Optional[datetime] = None
    follow_up_count: int = 0


class OutreachCreate(OutreachBase):
    pass


class OutreachUpdate(BaseModel):
    contact_id: Optional[int] = None
    post_id: Optional[int] = None
    channel: Optional[ChannelType] = None
    state: Optional[OutreachState] = None
    sent_at: Optional[datetime] = None
    follow_up_due: Optional[datetime] = None
    follow_up_count: Optional[int] = None


class OutreachRead(OutreachBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─── Message ───────────────────────────────────────────────


class MessageBase(BaseModel):
    outreach_id: int
    direction: MessageDirection
    content: str
    status: MessageStatus = MessageStatus.DRAFTED
    subject: Optional[str] = Field(None, max_length=200)


class MessageCreate(MessageBase):
    pass


class MessageUpdate(BaseModel):
    outreach_id: Optional[int] = None
    direction: Optional[MessageDirection] = None
    content: Optional[str] = None
    status: Optional[MessageStatus] = None


class MessageRead(MessageBase):
    id: int
    sent_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Setting ───────────────────────────────────────────────


class SettingBase(BaseModel):
    key: str = Field(..., max_length=100)
    value: str


class SettingCreate(SettingBase):
    pass


class SettingUpdate(BaseModel):
    value: str


class SettingRead(SettingBase):
    class Config:
        from_attributes = True
