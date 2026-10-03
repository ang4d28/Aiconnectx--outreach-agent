"""Dashboard "Needs attention" queue (backup for the WhatsApp alerts)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db import get_session
from app.db.models import (
    AttentionItem,
    AttentionStatus,
    AttentionType,
    Message,
    MessageStatus,
    ReviewStatus,
    User,
)
from app.workers.review import approve_message, cancel_message
from app.workers.send_queue import utc_now

router = APIRouter(prefix="/attention", tags=["attention"])


class AttentionRead(BaseModel):
    id: int
    type: AttentionType
    payload: dict
    status: AttentionStatus
    created_at: datetime | None = None


class AttentionPatch(BaseModel):
    draft: str | None = Field(None, min_length=1)
    status: AttentionStatus | None = None


def to_read(item: AttentionItem) -> AttentionRead:
    return AttentionRead(
        id=item.id,
        type=item.type,
        payload=item.payload or {},
        status=item.status,
        created_at=item.created_at,
    )


def get_item_or_404(db: Session, item_id: int) -> AttentionItem:
    item = db.get(AttentionItem, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attention item not found")
    return item


def require_open(item: AttentionItem) -> None:
    if item.status != AttentionStatus.OPEN:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Attention item is not open")


def linked_message(db: Session, item: AttentionItem) -> Message | None:
    message_id = (item.payload or {}).get("message_id")
    return db.get(Message, message_id) if message_id is not None else None


def is_unsent(message: Message | None) -> bool:
    return (
        message is not None
        and message.status != MessageStatus.SENT
        and message.review_status != ReviewStatus.CANCELLED
    )


@router.get("", response_model=list[AttentionRead])
def list_attention(
    status_filter: AttentionStatus | None = Query(AttentionStatus.OPEN, alias="status"),
    db: Session = Depends(get_session),
) -> list[AttentionRead]:
    stmt = select(AttentionItem).order_by(AttentionItem.id.desc())
    if status_filter is not None:
        stmt = stmt.where(AttentionItem.status == status_filter)
    return [to_read(item) for item in db.scalars(stmt)]


@router.post("/{item_id}/approve", response_model=AttentionRead)
def approve(
    item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> AttentionRead:
    """Approve the linked draft (it goes out on the next queue run) and resolve the item."""
    item = get_item_or_404(db, item_id)
    require_open(item)
    message = linked_message(db, item)
    if message is not None:
        if not is_unsent(message):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Message already sent or cancelled")
        approve_message(db, message, utc_now(), user.id)
    item.status = AttentionStatus.RESOLVED
    item.resolved_by = user.id
    db.commit()
    return to_read(item)


@router.post("/{item_id}/dismiss", response_model=AttentionRead)
def dismiss(
    item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> AttentionRead:
    """Dismiss the item; a linked unsent draft is cancelled and never sent."""
    item = get_item_or_404(db, item_id)
    require_open(item)
    message = linked_message(db, item)
    if is_unsent(message):
        cancel_message(db, message, utc_now(), user.id)
    item.status = AttentionStatus.DISMISSED
    item.resolved_by = user.id
    db.commit()
    return to_read(item)


@router.patch("/{item_id}", response_model=AttentionRead)
def update(
    item_id: int,
    data: AttentionPatch,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AttentionRead:
    item = get_item_or_404(db, item_id)
    if data.draft is not None:
        message = linked_message(db, item)
        if not is_unsent(message):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No editable draft on this item")
        message.edited_text = data.draft
        message.review_status = ReviewStatus.EDITED
        message.reviewed_at = utc_now()
        message.reviewed_by = user.id
        item.payload = {**(item.payload or {}), "draft": data.draft}
    if data.status is not None:
        item.status = data.status
        item.resolved_by = None if data.status == AttentionStatus.OPEN else user.id
    db.commit()
    return to_read(item)
