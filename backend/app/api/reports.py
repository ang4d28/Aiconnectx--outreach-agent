from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_session
from app.workers.reports import collect_stats
from app.workers.send_queue import utc_now

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/today")
def today(db: Session = Depends(get_session)) -> dict:
    """Same numbers as the 20:00 IST WhatsApp summary and the STATUS command."""
    return collect_stats(db, utc_now()).as_dict()
