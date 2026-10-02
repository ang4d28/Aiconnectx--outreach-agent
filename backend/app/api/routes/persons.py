from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import repository
from app.db import get_session
from app.db.schemas import (
    ContactCreate,
    ContactRead,
    ContactUpdate,
)

router = APIRouter(prefix="/persons", tags=["persons"])


@router.get("/", response_model=list[ContactRead])
def list_contacts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_session),
) -> list[ContactRead]:
    return repository.get_contacts(db, skip=skip, limit=limit)


@router.get("/{contact_id}", response_model=ContactRead)
def get_contact(
    contact_id: int,
    db: Session = Depends(get_session),
) -> ContactRead:
    contact = repository.get_contact(db, contact_id)
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return contact


@router.post("/", response_model=ContactRead, status_code=status.HTTP_201_CREATED)
def create_contact(
    data: ContactCreate,
    db: Session = Depends(get_session),
) -> ContactRead:
    return repository.create_contact(db, data)


@router.patch("/{contact_id}", response_model=ContactRead)
def update_contact(
    contact_id: int,
    data: ContactUpdate,
    db: Session = Depends(get_session),
) -> ContactRead:
    db_contact = repository.get_contact(db, contact_id)
    if not db_contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return repository.update_contact(db, db_contact, data)


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact(
    contact_id: int,
    db: Session = Depends(get_session),
) -> None:
    db_contact = repository.get_contact(db, contact_id)
    if not db_contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    repository.delete_contact(db, contact_id)
