from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import repository
from app.db import get_session
from app.db.schemas import (
    OutreachCreate,
    OutreachRead,
    OutreachUpdate,
)

router = APIRouter(prefix="/outreach", tags=["outreach"])


@router.get("/", response_model=list[OutreachRead])
def list_outreaches(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_session),
) -> list[OutreachRead]:
    return repository.get_outreaches(db, skip=skip, limit=limit)


@router.get("/{outreach_id}", response_model=OutreachRead)
def get_outreach(
    outreach_id: int,
    db: Session = Depends(get_session),
) -> OutreachRead:
    outreach = repository.get_outreach(db, outreach_id)
    if not outreach:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Outreach not found")
    return outreach


@router.post("/", response_model=OutreachRead, status_code=status.HTTP_201_CREATED)
def create_outreach(
    data: OutreachCreate,
    db: Session = Depends(get_session),
) -> OutreachRead:
    return repository.create_outreach(db, data)


@router.patch("/{outreach_id}", response_model=OutreachRead)
def update_outreach(
    outreach_id: int,
    data: OutreachUpdate,
    db: Session = Depends(get_session),
) -> OutreachRead:
    db_outreach = repository.get_outreach(db, outreach_id)
    if not db_outreach:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Outreach not found")
    return repository.update_outreach(db, db_outreach, data)


@router.delete("/{outreach_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_outreach(
    outreach_id: int,
    db: Session = Depends(get_session),
) -> None:
    db_outreach = repository.get_outreach(db, outreach_id)
    if not db_outreach:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Outreach not found")
    repository.delete_outreach(db, outreach_id)
