"""Hold window and owner review over WhatsApp.

Every outgoing message is held for HOLD_WINDOW_MINUTES with a WhatsApp preview.
The owner can SEND (release now), CANCEL (never send) or EDIT (replace text and
restart the hold). Without a reply the send queue auto-releases it once
hold_until passes. A hold that ends outside 10:00-17:00 IST moves to the next
window opening.
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterator

from sqlalchemy import func, select

from app.config import settings
from app.db.models import (
    AttentionItem,
    AttentionStatus,
    ChannelType,
    Company,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    NotificationLog,
    Outreach,
    ReviewStatus,
    Setting,
)
from app.workers.send_queue import (
    IST,
    PAUSE_KEY,
    REPLY_STATES,
    as_utc,
    in_send_window,
    ist_day_start,
    is_truthy,
    parse_hhmm,
    utc_now,
)

logger = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[str | None]]
CHANNEL_LABELS = {ChannelType.LINKEDIN: "LinkedIn", ChannelType.EMAIL: "Email"}
OPEN_REVIEW = {ReviewStatus.PENDING_REVIEW, ReviewStatus.EDITED}


async def whatsapp_notify(text: str) -> str | None:
    from app.channels import whatsapp

    return await whatsapp.send_text(text)


def message_text(message: Message) -> str:
    return message.edited_text or message.content


def next_send_time(at: datetime) -> datetime:
    """`at` if it falls inside the send window, else the next window opening."""
    if in_send_window(at):
        return as_utc(at)
    local = as_utc(at).astimezone(IST)
    start = parse_hhmm(settings.send_window_start)
    day = local.date() if local.time() < start else local.date() + timedelta(days=1)
    return datetime.combine(day, start, IST).astimezone(timezone.utc)


def hold_until_for(now: datetime) -> datetime:
    return next_send_time(as_utc(now) + timedelta(minutes=settings.hold_window_minutes))


def format_preview(
    message_id: int,
    channel: ChannelType,
    person: str,
    company: str,
    text: str,
    minutes: int | None,
) -> str:
    """minutes=None marks a draft that only goes out after SEND."""
    timing = f"Sends in {minutes} min." if minutes is not None else "Waits for your SEND."
    return (
        f"#{message_id} {CHANNEL_LABELS[channel]} to {person} ({company})\n"
        f'"{text}"\n'
        f"{timing}\n"
        f"Reply: SEND {message_id} | CANCEL {message_id} | EDIT {message_id} <new text>"
    )


def get_flag(db: Any, key: str) -> bool:
    setting = db.get(Setting, key)
    return is_truthy(setting.value if setting else None)


def set_flag(db: Any, key: str, value: bool) -> None:
    raw = "true" if value else "false"
    setting = db.get(Setting, key)
    if setting is None:
        db.add(Setting(key=key, value=raw))
    else:
        setting.value = raw
    db.commit()


class ReviewService:
    def __init__(
        self,
        db: Any,
        notify: Notify = whatsapp_notify,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.db = db
        self.notify = notify
        self.clock = clock

    async def start_hold(
        self, message: Message, channel: ChannelType, person: str, company: str
    ) -> datetime:
        now = self.clock()
        message.hold_until = hold_until_for(now)
        if message.review_status not in OPEN_REVIEW:
            message.review_status = ReviewStatus.PENDING_REVIEW
        minutes = math.ceil((message.hold_until - as_utc(now)).total_seconds() / 60)
        preview = format_preview(
            message.id, channel, person, company, message_text(message), minutes
        )
        try:
            message.whatsapp_message_id = await self.notify(preview)
            self.db.add(
                NotificationLog(type="review_preview", payload={"message_id": message.id})
            )
        except Exception as exc:
            # The hold still auto-releases; the dashboard is the backup.
            logger.error(f"WhatsApp preview failed for message {message.id}: {exc}")
        self.db.commit()
        return message.hold_until


def is_reply_draft(db: Any, message: Message) -> bool:
    """A reply drafted for the owner to approve (e.g. an answer to a question)."""
    if message.status != MessageStatus.DRAFTED:
        return False
    outreach = db.get(Outreach, message.outreach_id)
    return outreach is not None and outreach.state in REPLY_STATES


def resolve_attention_for(
    db: Any, message_id: int, status: AttentionStatus, user_id: int | None = None
) -> None:
    stmt = select(AttentionItem).where(
        AttentionItem.status == AttentionStatus.OPEN,
        AttentionItem.payload["message_id"].as_integer() == message_id,
    )
    for item in db.scalars(stmt):
        item.status = status
        item.resolved_by = user_id


def approve_message(db: Any, message: Message, now: datetime, user_id: int | None = None) -> None:
    """Release now: the queue sends it on its next run, within the guardrails.

    user_id is the dashboard user doing it (None for the owner on WhatsApp).
    """
    message.status = MessageStatus.APPROVED
    message.review_status = ReviewStatus.APPROVED
    message.reviewed_at = now
    message.reviewed_by = user_id
    message.hold_until = now
    resolve_attention_for(db, message.id, AttentionStatus.RESOLVED, user_id)
    db.commit()


def cancel_message(db: Any, message: Message, now: datetime, user_id: int | None = None) -> None:
    message.review_status = ReviewStatus.CANCELLED
    message.status = MessageStatus.REJECTED
    message.reviewed_at = now
    message.reviewed_by = user_id
    resolve_attention_for(db, message.id, AttentionStatus.DISMISSED, user_id)
    db.commit()


async def send_alert(db: Any, notify: Notify, text: str, message_id: int | None = None) -> None:
    """WhatsApp the owner; failures are logged, the dashboard is the backup."""
    try:
        await notify(text)
        db.add(NotificationLog(type="alert", payload={"message_id": message_id}))
    except Exception as exc:
        logger.error(f"WhatsApp alert failed: {exc}")
    db.commit()


def review_context(db: Any, message: Message) -> tuple[ChannelType, str, str]:
    outreach = db.get(Outreach, message.outreach_id)
    contact = db.get(Contact, outreach.contact_id) if outreach else None
    company = db.get(Company, contact.company_id) if contact else None
    channel = outreach.channel if outreach else ChannelType.EMAIL
    return (
        channel,
        contact.name if contact else "unknown",
        company.name if company else "unknown",
    )


class ReviewCommands:
    """Owner commands from the WhatsApp webhook (implements CommandHandler)."""

    def __init__(
        self,
        session_factory: Callable[[], Any] | None = None,
        notify: Notify = whatsapp_notify,
        clock: Callable[[], datetime] = utc_now,
    ):
        self._session_factory = session_factory
        self.notify = notify
        self.clock = clock

    @contextmanager
    def _session(self) -> Iterator[Any]:
        if self._session_factory is None:
            from app.db import SessionLocal

            self._session_factory = SessionLocal
        db = self._session_factory()
        try:
            yield db
        finally:
            db.close()

    def _reviewable(self, db: Any, message_id: int) -> tuple[Message | None, str | None]:
        message = db.get(Message, message_id)
        if message is None or message.direction != MessageDirection.OUTBOUND:
            return None, f"#{message_id} not found."
        if message.status == MessageStatus.SENT:
            return None, f"#{message_id} was already sent."
        if message.review_status == ReviewStatus.CANCELLED:
            return None, f"#{message_id} was already cancelled."
        if message.status != MessageStatus.APPROVED and not is_reply_draft(db, message):
            return None, f"#{message_id} is not queued for sending."
        return message, None

    async def send(self, message_id: int) -> str:
        with self._session() as db:
            message, error = self._reviewable(db, message_id)
            if error:
                return error
            approve_message(db, message, self.clock())
            return f"#{message_id} released. It goes out on the next queue run within send hours."

    async def cancel(self, message_id: int) -> str:
        with self._session() as db:
            message, error = self._reviewable(db, message_id)
            if error:
                return error
            cancel_message(db, message, self.clock())
            return f"#{message_id} cancelled. It will not be sent."

    async def edit(self, message_id: int, text: str) -> str:
        with self._session() as db:
            message, error = self._reviewable(db, message_id)
            if error:
                return error
            message.edited_text = text
            message.review_status = ReviewStatus.EDITED
            message.reviewed_at = self.clock()
            channel, person, company = review_context(db, message)
            if message.status == MessageStatus.DRAFTED:
                # Reply drafts (questions) never auto-release: no hold, just a new preview.
                db.commit()
                preview = format_preview(message.id, channel, person, company, text, None)
                await send_alert(db, self.notify, preview, message.id)
                return f"#{message_id} edited. Reply SEND {message_id} to send it."
            service = ReviewService(db, notify=self.notify, clock=self.clock)
            await service.start_hold(message, channel, person, company)
            return f"#{message_id} edited. Hold restarted."

    async def stop(self) -> str:
        with self._session() as db:
            set_flag(db, PAUSE_KEY, True)
        return "Paused. Nothing will be sent until START."

    async def start(self) -> str:
        with self._session() as db:
            set_flag(db, PAUSE_KEY, False)
        return "Resumed. Sending within guardrails."

    async def status(self) -> str:
        from app.workers.reports import collect_stats

        with self._session() as db:
            paused = get_flag(db, PAUSE_KEY)
            summary = collect_stats(db, self.clock()).text()
        state = "paused" if paused else "running"
        dry = " (DRY_RUN)" if settings.dry_run else ""
        return f"Status: {state}{dry}.\n{summary}"
