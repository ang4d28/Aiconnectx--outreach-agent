"""Public contact-form endpoint for the landing page (POST /api/inquiries).

This is the only API route meant to be reachable from the internet, so it is
defensive: strict validation, a honeypot field, a per-client rate limit and no
data in the response. A submission is stored, shown in "Needs attention" and
sent to the owner on WhatsApp.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from typing import Callable, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.db import get_session
from app.db.models import AttentionItem, AttentionType, Inquiry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inquiries", tags=["inquiries"])

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 3600
MAX_TRACKED_CLIENTS = 10_000


class InquiryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: str = Field(..., max_length=254)
    company: str = Field("", max_length=160)
    phone: str = Field("", max_length=40)
    interest: Literal["hire", "partner", "internships", "other"]
    message: str = Field(..., min_length=10, max_length=2000)
    website: str = Field("", max_length=200)  # honeypot: real visitors never fill it

    @field_validator("name", "email", "company", "phone", "message", mode="before")
    @classmethod
    def clean(cls, value: object) -> object:
        return CONTROL_CHARS.sub("", value).strip() if isinstance(value, str) else value

    @field_validator("name", "message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not EMAIL_PATTERN.match(value):
            raise ValueError("invalid email address")
        return value.lower()


class InquiryReceipt(BaseModel):
    status: str = "received"


class RateLimiter:
    """Sliding window per client, in memory (one backend process)."""

    def __init__(
        self,
        limit: int = RATE_LIMIT,
        window: float = RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit
        self.window = window
        self.clock = clock
        self.hits: dict[str, deque[float]] = {}

    def allow(self, client: str) -> bool:
        now = self.clock()
        if len(self.hits) > MAX_TRACKED_CLIENTS:
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}
        recent = self.hits.setdefault(client, deque())
        while recent and now - recent[0] >= self.window:
            recent.popleft()
        if len(recent) >= self.limit:
            return False
        recent.append(now)
        return True


limiter = RateLimiter()


def client_id(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or (request.client.host if request.client else "unknown")


def alert_text(inquiry: Inquiry) -> str:
    company = f" ({inquiry.company})" if inquiry.company else ""
    contact = inquiry.email + (f", {inquiry.phone}" if inquiry.phone else "")
    return (
        f"New website inquiry #{inquiry.id} from {inquiry.name}{company}\n"
        f"Interest: {inquiry.interest}. {contact}\n"
        f'"{inquiry.message[:500]}"'
    )


async def notify_owner(text: str) -> None:
    try:
        from app.workers.review import whatsapp_notify

        await whatsapp_notify(text)
    except Exception as exc:
        logger.error(f"WhatsApp alert for website inquiry failed: {exc}")


@router.post("", response_model=InquiryReceipt, status_code=status.HTTP_201_CREATED)
def create_inquiry(
    data: InquiryCreate,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_session),
) -> InquiryReceipt:
    if not limiter.allow(client_id(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests. Please try again later.")
    if data.website:
        return InquiryReceipt()  # looks successful to the bot; nothing is stored

    inquiry = Inquiry(
        name=data.name,
        email=data.email,
        company=data.company or None,
        phone=data.phone or None,
        interest=data.interest,
        message=data.message,
    )
    db.add(inquiry)
    db.flush()
    db.add(
        AttentionItem(
            type=AttentionType.NEEDS_MANUAL,
            payload={
                "reason": "New website inquiry",
                "inquiry_id": inquiry.id,
                "name": inquiry.name,
                "email": inquiry.email,
                "company": inquiry.company,
                "phone": inquiry.phone,
                "interest": inquiry.interest,
                "message": inquiry.message[:1000],
            },
        )
    )
    db.commit()
    background.add_task(notify_owner, alert_text(inquiry))
    return InquiryReceipt()
