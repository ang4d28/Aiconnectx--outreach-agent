from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import repository
from app.db import get_session
from app.db.schemas import (
    MessageCreate,
    MessageRead,
    MessageUpdate,
    OutreachRead,
)

router = APIRouter(prefix="/tracking", tags=["tracking"])


@router.get("/messages", response_model=list[MessageRead])
def list_messages(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_session),
) -> list[MessageRead]:
    return repository.get_messages(db, skip=skip, limit=limit)


@router.get("/messages/{message_id}", response_model=MessageRead)
def get_message(
    message_id: int,
    db: Session = Depends(get_session),
) -> MessageRead:
    message = repository.get_message(db, message_id)
    if not message:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    return message


@router.post("/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED)
def create_message(
    data: MessageCreate,
    db: Session = Depends(get_session),
) -> MessageRead:
    return repository.create_message(db, data)


@router.patch("/messages/{message_id}", response_model=MessageRead)
def update_message(
    message_id: int,
    data: MessageUpdate,
    db: Session = Depends(get_session),
) -> MessageRead:
    db_message = repository.get_message(db, message_id)
    if not db_message:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    return repository.update_message(db, db_message, data)


@router.delete("/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_message(
    message_id: int,
    db: Session = Depends(get_session),
) -> None:
    db_message = repository.get_message(db, message_id)
    if not db_message:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    repository.delete_message(db, message_id)


@router.get("/outreach/{contact_id}", response_model=list[OutreachRead])
def contact_history(
    contact_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_session),
) -> list[OutreachRead]:
    return repository.get_outreaches_by_contact(
        db, contact_id, skip=skip, limit=limit
    )
