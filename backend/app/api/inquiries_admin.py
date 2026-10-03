"""Dashboard view of website contact-form inquiries (the public POST is in inquiries.py)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dashboard import Pagination, iso, paginate
from app.db import get_session
from app.db.models import Inquiry

router = APIRouter(prefix="/inquiries", tags=["inquiries"])

InquiryStatus = Literal["new", "contacted", "closed"]


class InquiryPatch(BaseModel):
    status: InquiryStatus | None = None
    notes: str | None = Field(None, max_length=4000)


def to_dict(inquiry: Inquiry) -> dict[str, Any]:
    return {
        "id": inquiry.id,
        "name": inquiry.name,
        "email": inquiry.email,
        "company": inquiry.company,
        "phone": inquiry.phone,
        "interest": inquiry.interest,
        "message": inquiry.message,
        "status": inquiry.status,
        "notes": inquiry.notes or "",
        "created_at": iso(inquiry.created_at),
    }


@router.get("")
def list_inquiries(
    status_filter: InquiryStatus | None = Query(None, alias="status"),
    pg: Pagination = Depends(),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Inquiry).order_by(Inquiry.created_at.desc(), Inquiry.id.desc())
    if status_filter:
        stmt = stmt.where(Inquiry.status == status_filter)
    return paginate(db, stmt, pg, to_dict)


@router.get("/new-count")
def new_count(db: Session = Depends(get_session)) -> dict[str, int]:
    return {"new": db.scalar(select(func.count()).select_from(Inquiry).where(Inquiry.status == "new")) or 0}


@router.patch("/{inquiry_id}")
def update_inquiry(inquiry_id: int, data: InquiryPatch, db: Session = Depends(get_session)) -> dict[str, Any]:
    inquiry = db.get(Inquiry, inquiry_id)
    if inquiry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Inquiry not found")
    if data.status is not None:
        inquiry.status = data.status
    if data.notes is not None:
        inquiry.notes = data.notes.strip() or None
    db.commit()
    return to_dict(inquiry)
