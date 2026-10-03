"""Operator dashboard API (all routes require a signed-in user; roles are enforced by app.auth.deps).

Reads are paginated ({items, total, page, page_size}). Draft actions go through
the same helpers as the WhatsApp commands (workers/review.py), so the dashboard
and WhatsApp always act on the same state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import runtime_config
from app.channels.linkedin import DISABLED_KEY as LINKEDIN_DISABLED_KEY
from app.channels.linkedin import DISABLED_REASON_KEY as LINKEDIN_REASON_KEY
from app.auth.deps import get_current_user
from app.config import settings
from app.db import get_session
from app.db.models import (
    AttentionItem,
    AttentionStatus,
    AttentionType,
    ChannelType,
    Company,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    Post,
    ReviewStatus,
    Setting,
    User,
)
from app.workers.review import (
    OPEN_REVIEW,
    ReviewService,
    approve_message,
    cancel_message,
    format_preview,
    is_reply_draft,
    review_context,
    send_alert,
    whatsapp_notify,
)
from app.workers.send_queue import (
    EMAIL_DISABLED_KEY,
    IST,
    PAUSE_KEY,
    REPLY_STATES,
    as_utc,
    ist_day_start,
    is_truthy,
    utc_now,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

SHEETS_DISABLED_KEY = "sheets_disabled"
CHANNEL_KEYS = {
    "linkedin": LINKEDIN_DISABLED_KEY,
    "outlook": EMAIL_DISABLED_KEY,
    "sheets": SHEETS_DISABLED_KEY,
}
ACTIVITY_DAYS = 14
BULK_LIMIT = 100
MAX_QUERIES = 30
MAX_QUERY_LENGTH = 300
DraftView = Literal["awaiting", "held", "sent", "closed", "all"]

INTENT_BY_STATE: dict[OutreachState, str] = {
    OutreachState.INTERESTED: "interested",
    OutreachState.FUTURE_POOL: "no_opportunity",
    OutreachState.NO_OPPORTUNITY: "no_opportunity",
    OutreachState.WRONG_PERSON: "wrong_person",
    OutreachState.NOT_NOW: "not_now",
    OutreachState.UNSUBSCRIBED: "unsubscribe",
    OutreachState.REPLIED: "question",
}


# ─── Shared helpers ────────────────────────────────────────


class Pagination:
    def __init__(self, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        self.page = page
        self.page_size = page_size


def paginate(db: Session, stmt: Any, pg: Pagination, row_to_dict: Any) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.execute(stmt.offset((pg.page - 1) * pg.page_size).limit(pg.page_size)).all()
    return {
        "items": [row_to_dict(*row) for row in rows],
        "total": total,
        "page": pg.page,
        "page_size": pg.page_size,
    }


def iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def get_flag(db: Session, key: str) -> bool:
    row = db.get(Setting, key)
    return is_truthy(row.value if row else None)


def put_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


# ─── Status, pause, channels ───────────────────────────────


def disabled_channels(db: Session) -> list[dict[str, str | None]]:
    errors = list(
        db.scalars(
            select(AttentionItem)
            .where(AttentionItem.type == AttentionType.CHANNEL_ERROR, AttentionItem.status == AttentionStatus.OPEN)
            .order_by(AttentionItem.id.desc())
            .limit(50)
        )
    )
    result: list[dict[str, str | None]] = []
    for channel, key in CHANNEL_KEYS.items():
        if not get_flag(db, key):
            continue
        reason = db.get(Setting, LINKEDIN_REASON_KEY) if channel == "linkedin" else None
        text = reason.value if reason and reason.value else None
        if text is None:
            match = next((e for e in errors if (e.payload or {}).get("channel") == channel), None)
            text = (match.payload or {}).get("error") or (match.payload or {}).get("reason") if match else None
        result.append({"channel": channel, "reason": text})
    return result


@router.get("/status")
def get_status(db: Session = Depends(get_session)) -> dict[str, Any]:
    counts = {
        kind.value: count
        for kind, count in db.execute(
            select(AttentionItem.type, func.count(AttentionItem.id))
            .where(AttentionItem.status == AttentionStatus.OPEN)
            .group_by(AttentionItem.type)
        ).all()
    }
    return {
        "paused": get_flag(db, PAUSE_KEY),
        "dry_run": settings.dry_run,
        "disabled_channels": disabled_channels(db),
        "open_attention": counts,
    }


class PauseRequest(BaseModel):
    paused: bool


@router.post("/pause")
def set_pause(data: PauseRequest, db: Session = Depends(get_session)) -> dict[str, bool]:
    put_setting(db, PAUSE_KEY, "true" if data.paused else "false")
    db.commit()
    return {"paused": data.paused}


@router.post("/channels/{channel}/enable")
def enable_channel(channel: str, db: Session = Depends(get_session)) -> dict[str, str]:
    """Re-enable a channel that was disabled by repeated errors and resolve its alerts."""
    if channel not in CHANNEL_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown channel")
    put_setting(db, CHANNEL_KEYS[channel], "false")
    if channel == "linkedin":
        put_setting(db, LINKEDIN_REASON_KEY, "")
    for row in db.scalars(select(Setting).where(Setting.key.like("job_failures:%"))):
        row.value = "0"  # otherwise the failure counter never reaches the threshold again
    for item in db.scalars(
        select(AttentionItem).where(
            AttentionItem.type == AttentionType.CHANNEL_ERROR, AttentionItem.status == AttentionStatus.OPEN
        )
    ):
        if (item.payload or {}).get("channel") == channel:
            item.status = AttentionStatus.RESOLVED
    db.commit()
    return {"channel": channel, "status": "enabled"}


# ─── Overview ──────────────────────────────────────────────


def day_buckets(now: datetime) -> list[Any]:
    today = as_utc(now).astimezone(IST).date()
    return [today - timedelta(days=offset) for offset in range(ACTIVITY_DAYS - 1, -1, -1)]


def build_activity(
    now: datetime, leads: list[datetime], sent: list[datetime], replies: list[datetime]
) -> list[dict[str, Any]]:
    days = day_buckets(now)
    table = {day: {"date": day.isoformat(), "leads": 0, "sent": 0, "replies": 0} for day in days}
    for key, stamps in (("leads", leads), ("sent", sent), ("replies", replies)):
        for stamp in stamps:
            day = as_utc(stamp).astimezone(IST).date()
            if day in table:
                table[day][key] += 1
    return list(table.values())


def funnel_counts(db: Session) -> list[dict[str, Any]]:
    def distinct_outreaches(*conditions: Any) -> int:
        return db.scalar(
            select(func.count(func.distinct(Message.outreach_id))).where(*conditions)
        ) or 0

    leads = db.scalar(select(func.count(Outreach.id))) or 0
    drafted = distinct_outreaches(Message.direction == MessageDirection.OUTBOUND)
    sent = distinct_outreaches(
        Message.direction == MessageDirection.OUTBOUND, Message.status == MessageStatus.SENT
    )
    replied = distinct_outreaches(Message.direction == MessageDirection.INBOUND)
    interested = db.scalar(
        select(func.count(Outreach.id)).where(Outreach.state == OutreachState.INTERESTED)
    ) or 0
    return [
        {"stage": "Leads", "count": leads},
        {"stage": "Drafted", "count": drafted},
        {"stage": "Sent", "count": sent},
        {"stage": "Replied", "count": replied},
        {"stage": "Interested", "count": interested},
    ]


@router.get("/overview")
def overview(db: Session = Depends(get_session)) -> dict[str, Any]:
    from app.workers.reports import collect_stats

    now = utc_now()
    start = ist_day_start(now)
    window_start = start - timedelta(days=ACTIVITY_DAYS - 1)
    stats = collect_stats(db, now)
    cards = {
        "leads_today": db.scalar(select(func.count(Outreach.id)).where(Outreach.created_at >= start)) or 0,
        "drafts_pending": db.scalar(
            select(func.count(Message.id)).where(
                Message.direction == MessageDirection.OUTBOUND, Message.status == MessageStatus.DRAFTED
            )
        )
        or 0,
        "sent_today": stats.total_sent,
        "replies_today": stats.replies,
        "interested_today": stats.interested,
    }
    leads = list(db.scalars(select(Outreach.created_at).where(Outreach.created_at >= window_start)))
    sent = list(
        db.scalars(
            select(Message.sent_at).where(
                Message.direction == MessageDirection.OUTBOUND,
                Message.status == MessageStatus.SENT,
                Message.sent_at >= window_start,
            )
        )
    )
    replies = list(
        db.scalars(
            select(Message.sent_at).where(
                Message.direction == MessageDirection.INBOUND, Message.sent_at >= window_start
            )
        )
    )
    return {
        "cards": cards,
        "activity": build_activity(now, leads, sent, replies),
        "funnel": funnel_counts(db),
        "caps": {"sent": stats.sent, "limits": stats.caps},
    }


# ─── Leads ─────────────────────────────────────────────────


def leads_statement(state: str | None, channel: str | None, source: str | None, q: str | None) -> Any:
    stmt = (
        select(Outreach, Contact, Company, Post.url)
        .join(Contact, Contact.id == Outreach.contact_id)
        .join(Company, Company.id == Contact.company_id, isouter=True)
        .join(Post, Post.id == Outreach.post_id, isouter=True)
    )
    if state:
        stmt = stmt.where(Outreach.state == OutreachState(state))
    if channel:
        stmt = stmt.where(Outreach.channel == ChannelType(channel))
    if source == "post":
        stmt = stmt.where(Outreach.post_id.is_not(None))
    elif source == "search":
        stmt = stmt.where(Outreach.post_id.is_(None))
    if q and q.strip():
        pattern = like_pattern(q.strip())
        stmt = stmt.where(
            or_(
                Contact.name.ilike(pattern, escape="\\"),
                Company.name.ilike(pattern, escape="\\"),
                Contact.email.ilike(pattern, escape="\\"),
                Contact.title.ilike(pattern, escape="\\"),
            )
        )
    return stmt.order_by(Outreach.id.desc())


def lead_row(outreach: Outreach, contact: Contact, company: Company | None, post_url: str | None) -> dict[str, Any]:
    return {
        "id": outreach.id,
        "person": contact.name,
        "title": contact.title,
        "email": contact.email,
        "linkedin_url": contact.linkedin_url,
        "company": company.name if company else None,
        "channel": outreach.channel.value,
        "status": outreach.state.value,
        "source": "post" if outreach.post_id else "search",
        "post_url": post_url,
        "sent_at": iso(outreach.sent_at),
        "follow_up_due": iso(outreach.follow_up_due),
        "created_at": iso(outreach.created_at),
    }


@router.get("/leads")
def list_leads(
    pg: Pagination = Depends(),
    state: OutreachState | None = Query(None, alias="status"),
    channel: ChannelType | None = None,
    source: Literal["post", "search"] | None = None,
    q: str | None = Query(None, max_length=100),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    stmt = leads_statement(state.value if state else None, channel.value if channel else None, source, q)
    return paginate(db, stmt, pg, lead_row)


def message_row(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "direction": message.direction.value,
        "text": message.edited_text or message.content,
        "status": message.status.value,
        "review_status": message.review_status.value if message.review_status else None,
        "at": iso(message.sent_at if message.status == MessageStatus.SENT else message.created_at),
        "created_at": iso(message.created_at),
    }


@router.get("/leads/{outreach_id}")
def lead_detail(outreach_id: int, db: Session = Depends(get_session)) -> dict[str, Any]:
    row = db.execute(leads_statement(None, None, None, None).where(Outreach.id == outreach_id)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    outreach, contact, company, post_url = row
    post = db.get(Post, outreach.post_id) if outreach.post_id else None
    messages = list(
        db.scalars(select(Message).where(Message.outreach_id == outreach_id).order_by(Message.id))
    )
    return {
        **lead_row(outreach, contact, company, post_url),
        "follow_up_count": outreach.follow_up_count,
        "post": (
            {
                "url": post.url,
                "roles": post.roles or [],
                "text": (post.raw_text or "")[:2000],
                "relevance": post.relevance_score,
                "poster": post.poster_name,
            }
            if post
            else None
        ),
        "messages": [message_row(m) for m in messages],
    }


# ─── Drafts ────────────────────────────────────────────────


def drafts_statement(view: str) -> Any:
    stmt = (
        select(Message, Outreach, Contact, Company)
        .join(Outreach, Outreach.id == Message.outreach_id)
        .join(Contact, Contact.id == Outreach.contact_id)
        .join(Company, Company.id == Contact.company_id, isouter=True)
        .where(Message.direction == MessageDirection.OUTBOUND)
    )
    conditions = {
        "awaiting": [Message.status == MessageStatus.DRAFTED],
        "held": [Message.status == MessageStatus.APPROVED],
        "sent": [Message.status == MessageStatus.SENT],
        "closed": [Message.status.in_((MessageStatus.REJECTED, MessageStatus.FAILED, MessageStatus.BLOCKED))],
        "all": [],
    }[view]
    return stmt.where(*conditions).order_by(Message.id.desc())


def draft_row(message: Message, outreach: Outreach, contact: Contact, company: Company | None) -> dict[str, Any]:
    return {
        "id": message.id,
        "outreach_id": outreach.id,
        "person": contact.name,
        "company": company.name if company else None,
        "channel": outreach.channel.value,
        "status": message.status.value,
        "review_status": message.review_status.value if message.review_status else None,
        "text": message.edited_text or message.content,
        "edited": bool(message.edited_text),
        "hold_until": iso(message.hold_until),
        "is_reply": outreach.state in REPLY_STATES,
        "created_at": iso(message.created_at),
    }


@router.get("/drafts")
def list_drafts(
    pg: Pagination = Depends(),
    view: DraftView = "awaiting",
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    return paginate(db, drafts_statement(view), pg, draft_row)


def get_draft_or_404(db: Session, draft_id: int) -> Message:
    message = db.get(Message, draft_id)
    if message is None or message.direction != MessageDirection.OUTBOUND:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    return message


def release_or_approve(db: Session, message: Message, user_id: int | None = None) -> str:
    """Approve a draft, or release a held message now. Returns what happened."""
    if message.status == MessageStatus.DRAFTED:
        if is_reply_draft(db, message):
            approve_message(db, message, utc_now(), user_id)  # replies go out on the next queue run
            return "released"
        message.status = MessageStatus.APPROVED  # the queue starts the hold and the WhatsApp preview
        message.reviewed_by = user_id
        db.commit()
        return "approved"
    if message.status == MessageStatus.APPROVED and message.review_status in OPEN_REVIEW:
        approve_message(db, message, utc_now(), user_id)
        return "released"
    raise HTTPException(status.HTTP_409_CONFLICT, "This message can no longer be approved")


@router.post("/drafts/{draft_id}/approve")
def approve_draft(
    draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> dict[str, str]:
    message = get_draft_or_404(db, draft_id)
    return {"id": str(draft_id), "result": release_or_approve(db, message, user.id)}


@router.post("/drafts/{draft_id}/cancel")
def cancel_draft(
    draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> dict[str, str]:
    message = get_draft_or_404(db, draft_id)
    if message.status in (
        MessageStatus.SENT,
        MessageStatus.REJECTED,
        MessageStatus.FAILED,
        MessageStatus.BLOCKED,
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "This message can no longer be cancelled")
    cancel_message(db, message, utc_now(), user.id)
    return {"id": str(draft_id), "result": "cancelled"}


class DraftEdit(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.patch("/drafts/{draft_id}")
async def edit_draft(
    draft_id: int, data: DraftEdit, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> dict[str, str]:
    message = get_draft_or_404(db, draft_id)
    text = data.text.strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Text must not be blank")
    if message.status == MessageStatus.DRAFTED and not is_reply_draft(db, message):
        message.content = text
        message.edited_text = None
        message.reviewed_by = user.id
        db.commit()
        return {"id": str(draft_id), "result": "edited"}
    if message.status not in (MessageStatus.DRAFTED, MessageStatus.APPROVED):
        raise HTTPException(status.HTTP_409_CONFLICT, "This message can no longer be edited")
    message.edited_text = text
    message.review_status = ReviewStatus.EDITED
    message.reviewed_at = utc_now()
    message.reviewed_by = user.id
    channel, person, company = review_context(db, message)
    if message.status == MessageStatus.APPROVED and message.hold_until is not None:
        await ReviewService(db).start_hold(message, channel, person, company)  # new hold + preview
        return {"id": str(draft_id), "result": "hold_restarted"}
    db.commit()
    if message.status == MessageStatus.DRAFTED:
        await send_alert(db, whatsapp_notify, format_preview(message.id, channel, person, company, text, None), message.id)
    return {"id": str(draft_id), "result": "edited"}


class BulkApprove(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=BULK_LIMIT)


@router.post("/drafts/bulk-approve")
def bulk_approve(
    data: BulkApprove, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> dict[str, Any]:
    approved: list[int] = []
    skipped: list[dict[str, Any]] = []
    for draft_id in dict.fromkeys(data.ids):
        message = db.get(Message, draft_id)
        if message is None or message.direction != MessageDirection.OUTBOUND:
            skipped.append({"id": draft_id, "reason": "not found"})
            continue
        try:
            release_or_approve(db, message, user.id)
            approved.append(draft_id)
        except HTTPException as exc:
            skipped.append({"id": draft_id, "reason": exc.detail})
    return {"approved": approved, "skipped": skipped}


# ─── Replies ───────────────────────────────────────────────


def replies_statement() -> Any:
    latest = (
        select(func.max(Message.id).label("message_id"))
        .where(Message.direction == MessageDirection.INBOUND)
        .group_by(Message.outreach_id)
        .subquery()
    )
    return (
        select(Message, Outreach, Contact, Company)
        .join(latest, latest.c.message_id == Message.id)
        .join(Outreach, Outreach.id == Message.outreach_id)
        .join(Contact, Contact.id == Outreach.contact_id)
        .join(Company, Company.id == Contact.company_id, isouter=True)
        .order_by(Message.id.desc())
    )


def reply_row(message: Message, outreach: Outreach, contact: Contact, company: Company | None) -> dict[str, Any]:
    return {
        "outreach_id": outreach.id,
        "message_id": message.id,
        "person": contact.name,
        "company": company.name if company else None,
        "channel": outreach.channel.value,
        "text": message.content,
        "received_at": iso(message.sent_at or message.created_at),
        "status": outreach.state.value,
        "intent": INTENT_BY_STATE.get(outreach.state, "other"),
    }


@router.get("/replies")
def list_replies(pg: Pagination = Depends(), db: Session = Depends(get_session)) -> dict[str, Any]:
    """Latest reply per lead. The intent is derived from the lead's resulting state."""
    return paginate(db, replies_statement(), pg, reply_row)


# ─── Needs attention ───────────────────────────────────────


def attention_row(item: AttentionItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type.value,
        "status": item.status.value,
        "payload": item.payload or {},
        "created_at": iso(item.created_at),
    }


@router.get("/attention")
def list_attention(
    pg: Pagination = Depends(),
    item_status: AttentionStatus | Literal["all"] = Query(AttentionStatus.OPEN, alias="status"),
    item_type: AttentionType | None = Query(None, alias="type"),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(AttentionItem)
    if item_status != "all":
        stmt = stmt.where(AttentionItem.status == item_status)
    if item_type:
        stmt = stmt.where(AttentionItem.type == item_type)
    stmt = stmt.order_by(AttentionItem.id.desc())
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(stmt.offset((pg.page - 1) * pg.page_size).limit(pg.page_size))
    return {
        "items": [attention_row(item) for item in rows],
        "total": total,
        "page": pg.page,
        "page_size": pg.page_size,
    }


# ─── Settings ──────────────────────────────────────────────


def load_queries(db: Session) -> list[str]:
    from app.workers.post_monitor import PostMonitor

    return PostMonitor(db=db)._load_queries()


def settings_payload(db: Session) -> dict[str, Any]:
    return {"values": runtime_config.current_values(), "search_queries": load_queries(db)}


class SettingsUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    confirm_live: bool = False


@router.get("/settings")
def get_settings(db: Session = Depends(get_session)) -> dict[str, Any]:
    return settings_payload(db)


@router.put("/settings")
def update_settings(data: SettingsUpdate, db: Session = Depends(get_session)) -> dict[str, Any]:
    clean, errors = runtime_config.validate_changes(data.values)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"errors": errors})
    if clean.get("dry_run") is False and settings.dry_run and not data.confirm_live:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Turning DRY_RUN off sends real messages. Confirm to go live.",
        )
    runtime_config.save_overrides(db, clean)
    logger.warning(f"Dashboard settings changed: {', '.join(sorted(clean)) or 'nothing'}")
    return settings_payload(db)


class QueriesUpdate(BaseModel):
    queries: list[str] = Field(..., min_length=1, max_length=MAX_QUERIES)


@router.put("/search-queries")
def update_queries(data: QueriesUpdate, db: Session = Depends(get_session)) -> dict[str, Any]:
    queries = [q.strip() for q in data.queries if q.strip()]
    if not queries:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Add at least one search query")
    too_long = [q for q in queries if len(q) > MAX_QUERY_LENGTH]
    if too_long:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Queries must be at most {MAX_QUERY_LENGTH} characters"
        )
    put_setting(db, runtime_config.SEARCH_QUERIES_KEY, json.dumps(list(dict.fromkeys(queries))))
    db.commit()
    return settings_payload(db)
