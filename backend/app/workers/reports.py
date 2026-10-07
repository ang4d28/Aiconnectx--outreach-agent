"""Send notifications and the daily summary, all over WhatsApp.

After each successful send the owner gets one line. When more than
BATCH_THRESHOLD sends land within BATCH_WINDOW, the notices are held and
flushed as one batched message (the send queue flushes at the end of each run).
Every notice and summary is logged in notifications_log. The same summary backs
the STATUS command and GET /reports/today.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable

from sqlalchemy import func, select

from app.db.models import (
    AttentionItem,
    AttentionStatus,
    AttentionType,
    ChannelType,
    Message,
    MessageDirection,
    MessageStatus,
    NotificationLog,
    Outreach,
    ReviewStatus,
)
from app.workers.review import CHANNEL_LABELS, Notify, whatsapp_notify
from app.workers.send_queue import IST, as_utc, daily_cap, ist_day_start, utc_now

logger = logging.getLogger(__name__)

BATCH_THRESHOLD = 5
BATCH_WINDOW = timedelta(minutes=10)
SUMMARY_TIME = time(20, 0)
PENDING = "send_pending"
SENT_NOTICE = "send"
SEND_BATCH = "send_batch"
DAILY_SUMMARY = "daily_summary"
OPEN_REVIEW = (ReviewStatus.PENDING_REVIEW, ReviewStatus.EDITED)


def send_line(message_id: int, channel: str, person: str, company: str) -> str:
    return f"Sent #{message_id} via {CHANNEL_LABELS[ChannelType(channel)]} to {person} ({company})"


def batch_text(payloads: list[dict]) -> str:
    lines = [send_line(p["message_id"], p["channel"], p["person"], p["company"]) for p in payloads]
    return f"Sent {len(lines)} messages:\n" + "\n".join(lines)


class SendReporter:
    def __init__(
        self,
        db: Any,
        notify: Notify = whatsapp_notify,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.db = db
        self.notify = notify
        self.clock = clock

    def _pending(self) -> list[NotificationLog]:
        stmt = select(NotificationLog).where(NotificationLog.type == PENDING).order_by(NotificationLog.id)
        return list(self.db.scalars(stmt))

    def _recent_sends(self, now: datetime) -> int:
        stmt = select(func.count(NotificationLog.id)).where(
            NotificationLog.type.in_((PENDING, SENT_NOTICE)),
            NotificationLog.sent_at >= as_utc(now) - BATCH_WINDOW,
        )
        return self.db.scalar(stmt) or 0

    async def record_send(
        self, message_id: int, channel: ChannelType, person: str, company: str
    ) -> None:
        """Log a send; notify now unless a burst is under way. Never raises."""
        try:
            now = self.clock()
            self.db.add(
                NotificationLog(
                    type=PENDING,
                    payload={
                        "message_id": message_id,
                        "channel": channel.value,
                        "person": person,
                        "company": company,
                    },
                    sent_at=now,
                )
            )
            self.db.commit()
            if self._recent_sends(now) <= BATCH_THRESHOLD:
                await self.flush()
        except Exception as exc:
            logger.error(f"Send notice failed for message {message_id}: {exc}")

    async def flush(self) -> int:
        """Send everything pending: one line each, or one batched message for several."""
        pending = self._pending()
        if not pending:
            return 0
        payloads = [dict(row.payload) for row in pending]
        try:
            if len(pending) == 1:
                await self.notify(send_line(**{k: payloads[0][k] for k in
                                               ("message_id", "channel", "person", "company")}))
            else:
                await self.notify(batch_text(payloads))
        except Exception as exc:
            logger.error(f"WhatsApp send notice failed, will retry: {exc}")
            return 0
        for row in pending:
            row.type = SENT_NOTICE
        if len(pending) > 1:
            self.db.add(
                NotificationLog(
                    type=SEND_BATCH,
                    payload={"message_ids": [p["message_id"] for p in payloads]},
                    sent_at=self.clock(),
                )
            )
        self.db.commit()
        return len(pending)


# ─── Daily summary ─────────────────────────────────────────


@dataclass
class Stats:
    date: str
    sent: dict[str, int] = field(default_factory=dict)
    caps: dict[str, int] = field(default_factory=dict)
    replies: int = 0
    interested: int = 0
    pending_reviews: int = 0
    channel_errors: int = 0

    @property
    def total_sent(self) -> int:
        return sum(self.sent.values())

    def text(self) -> str:
        label = {c.value: CHANNEL_LABELS[c] for c in ChannelType}
        by_channel = ", ".join(f"{label[c]} {n}" for c, n in self.sent.items())
        caps = ", ".join(f"{self.sent[c]}/{self.caps[c]} {label[c]}" for c in self.caps)
        return (
            f"Daily summary, {self.date}\n"
            f"Sent today: {self.total_sent} ({by_channel})\n"
            f"Caps used: {caps}\n"
            f"Replies: {self.replies}, interested: {self.interested}\n"
            f"Pending reviews: {self.pending_reviews}\n"
            f"Channel errors: {self.channel_errors}"
        )

    def as_dict(self) -> dict:
        return {**asdict(self), "total_sent": self.total_sent, "text": self.text()}


def collect_stats(db: Any, now: datetime) -> Stats:
    """Today's numbers in four queries: sends per channel, replies, held messages, attention items."""
    start = ist_day_start(now)
    stats = Stats(date=as_utc(now).astimezone(IST).strftime("%a %d %b"))
    per_channel = {
        channel: (sent, first)
        for channel, sent, first in db.execute(
            select(
                Outreach.channel,
                func.count(Message.id).filter(Message.sent_at >= start),
                func.min(Message.sent_at),
            )
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(Message.status == MessageStatus.SENT, Message.direction == MessageDirection.OUTBOUND)
            .group_by(Outreach.channel)
        ).all()
    }
    for channel in ChannelType:
        sent, first_send = per_channel.get(channel, (0, None))
        stats.sent[channel.value] = sent or 0
        stats.caps[channel.value] = daily_cap(channel, first_send, now)
    stats.replies = db.scalar(
        select(func.count(Message.id)).where(
            Message.direction == MessageDirection.INBOUND, Message.sent_at >= start
        )
    ) or 0
    held = db.scalar(
        select(func.count(Message.id)).where(
            Message.status == MessageStatus.APPROVED,
            Message.direction == MessageDirection.OUTBOUND,
            Message.review_status.in_(OPEN_REVIEW),
        )
    ) or 0
    is_open = AttentionItem.status == AttentionStatus.OPEN
    interested, open_questions, channel_errors = db.execute(
        select(
            func.count(AttentionItem.id).filter(
                AttentionItem.type == AttentionType.INTERESTED, AttentionItem.created_at >= start
            ),
            func.count(AttentionItem.id).filter(AttentionItem.type == AttentionType.QUESTION, is_open),
            func.count(AttentionItem.id).filter(AttentionItem.type == AttentionType.CHANNEL_ERROR, is_open),
        )
    ).one()
    stats.interested = interested or 0
    stats.pending_reviews = held + (open_questions or 0)
    stats.channel_errors = channel_errors or 0
    return stats


def summary_due(now: datetime, last_sent: datetime | None) -> bool:
    """True from 20:00 IST once per IST day."""
    local = as_utc(now).astimezone(IST)
    if local.time() < SUMMARY_TIME:
        return False
    return last_sent is None or as_utc(last_sent) < ist_day_start(now)


async def send_daily_summary_if_due(
    db: Any,
    notify: Notify = whatsapp_notify,
    now: datetime | None = None,
) -> bool:
    """Called by the scheduler; sends at most one summary per IST day."""
    now = now or utc_now()
    last_sent = db.scalar(
        select(func.max(NotificationLog.sent_at)).where(NotificationLog.type == DAILY_SUMMARY)
    )
    if not summary_due(now, last_sent):
        return False
    stats = collect_stats(db, now)
    try:
        await notify(stats.text())
    except Exception as exc:
        logger.error(f"Daily summary failed, will retry: {exc}")
        return False
    db.add(NotificationLog(type=DAILY_SUMMARY, payload=stats.as_dict(), sent_at=now))
    db.commit()
    return True
