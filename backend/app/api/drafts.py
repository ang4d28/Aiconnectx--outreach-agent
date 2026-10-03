from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.config import settings
from app.db import get_session, repository
from app.db.models import ChannelType, Message, MessageStatus, Outreach, User
from app.db.schemas import MessageUpdate

router = APIRouter(prefix="/drafts", tags=["drafts"])


class DraftRead(BaseModel):
    id: int
    outreach_id: int
    channel: ChannelType
    content: str
    status: MessageStatus
    dry_run: bool


class DraftEdit(BaseModel):
    content: str = Field(..., min_length=1)


def to_read(message: Message, channel: ChannelType) -> DraftRead:
    return DraftRead(
        id=message.id,
        outreach_id=message.outreach_id,
        channel=channel,
        content=message.content,
        status=message.status,
        dry_run=settings.dry_run,
    )


def get_draft_or_404(db: Session, draft_id: int) -> Message:
    message = repository.get_message(db, draft_id)
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    return message


def require_pending(message: Message) -> None:
    if message.status != MessageStatus.DRAFTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Draft is already {message.status.value}"
        )


def channel_of(db: Session, message: Message) -> ChannelType:
    return repository.get_outreach(db, message.outreach_id).channel


def set_status(db: Session, message: Message, new_status: MessageStatus, user: User) -> DraftRead:
    require_pending(message)
    updated = repository.update_message(db, message, MessageUpdate(status=new_status))
    return record_reviewer(db, updated, user)


def record_reviewer(db: Session, message: Message, user: User) -> DraftRead:
    message.reviewed_by = user.id
    db.commit()
    return to_read(message, channel_of(db, message))


@router.get("", response_model=list[DraftRead])
def list_drafts(
    draft_status: MessageStatus = Query(MessageStatus.DRAFTED, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_session),
) -> list[DraftRead]:
    stmt = (
        select(Message, Outreach.channel)
        .join(Outreach, Outreach.id == Message.outreach_id)
        .where(Message.status == draft_status)
        .order_by(Message.id)
        .offset(skip)
        .limit(limit)
    )
    return [to_read(message, channel) for message, channel in db.execute(stmt)]


@router.post("/{draft_id}/approve", response_model=DraftRead)
def approve_draft(
    draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> DraftRead:
    return set_status(db, get_draft_or_404(db, draft_id), MessageStatus.APPROVED, user)


@router.post("/{draft_id}/reject", response_model=DraftRead)
def reject_draft(
    draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> DraftRead:
    return set_status(db, get_draft_or_404(db, draft_id), MessageStatus.REJECTED, user)


@router.patch("/{draft_id}", response_model=DraftRead)
def edit_draft(
    draft_id: int,
    data: DraftEdit,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> DraftRead:
    message = get_draft_or_404(db, draft_id)
    require_pending(message)
    updated = repository.update_message(db, message, MessageUpdate(content=data.content))
    return record_reviewer(db, updated, user)
