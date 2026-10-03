"""Follow-ups and rechecks.

- CONTACTED / DM_SENT: follow-ups on day 4 and day 10 on the same channel; with
  no reply after the last one the outreach is CLOSED (the send queue sets the
  close date when the last follow-up goes out).
- NOT_NOW (30 days) and FUTURE_POOL (60 days): one in-thread check-in.

Messages are queued as approved, so they go through the hold window and every
send-queue guardrail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import func, select

from app.db import SessionLocal
from app.db.models import (
    Company,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    ReviewStatus,
)
from app.workers.replies import first_name
from app.workers.send_queue import FOLLOW_UP_DAYS, utc_now

logger = logging.getLogger(__name__)

FOLLOW_UP_STATES = (OutreachState.CONTACTED, OutreachState.DM_SENT)
RECHECK_STATES = (OutreachState.NOT_NOW, OutreachState.FUTURE_POOL)
DUE_STATES = FOLLOW_UP_STATES + RECHECK_STATES

FOLLOW_UP_TEXTS = (
    "Hi {first}, following up on my note about AI ConnectX. We'd be glad to share profiles "
    "of B.Tech AI/ML students who could fit roles at {company}. Would a quick call work?",
    "Hi {first}, one last note from me. If campus hiring for AI/ML roles is on the roadmap "
    "at {company}, an MoU with AI ConnectX keeps you first in line for our students. "
    "Happy to send details.",
)
RECHECK_TEXTS = {
    OutreachState.NOT_NOW: (
        "Hi {first}, checking back in as promised. Is now a better time to talk about "
        "AI/ML hiring at {company}?"
    ),
    OutreachState.FUTURE_POOL: (
        "Hi {first}, checking in again. Have any AI/ML or software roles opened up at "
        "{company}? Our MoU offer still stands."
    ),
}


@dataclass
class FollowUpReport:
    queued: int = 0
    closed: int = 0
    skipped: int = 0


class FollowUpWorker:
    def __init__(self, db: Any = None, clock: Callable[[], datetime] = utc_now):
        self.db = db or SessionLocal()
        self.clock = clock

    def _due_outreaches(self, now: datetime) -> list[Outreach]:
        stmt = select(Outreach).where(
            Outreach.follow_up_due.is_not(None),
            Outreach.follow_up_due <= now,
            Outreach.state.in_(DUE_STATES),
        )
        return list(self.db.scalars(stmt))

    def _has_pending(self, outreach_id: int) -> bool:
        stmt = select(func.count(Message.id)).where(
            Message.outreach_id == outreach_id,
            Message.direction == MessageDirection.OUTBOUND,
            Message.status.in_((MessageStatus.DRAFTED, MessageStatus.APPROVED)),
        )
        return (self.db.scalar(stmt) or 0) > 0

    def _text(self, template: str, outreach: Outreach) -> str | None:
        contact = self.db.get(Contact, outreach.contact_id)
        if contact is None or contact.is_unsubscribed:
            return None
        company = self.db.get(Company, contact.company_id)
        return template.format(
            first=first_name(contact.name), company=company.name if company else "your team"
        )

    def run_once(self) -> FollowUpReport:
        report = FollowUpReport()
        now = self.clock()
        for outreach in self._due_outreaches(now):
            if self._has_pending(outreach.id):
                report.skipped += 1
                continue
            if outreach.state in FOLLOW_UP_STATES:
                count = outreach.follow_up_count or 0
                if count >= len(FOLLOW_UP_DAYS):
                    outreach.state = OutreachState.CLOSED
                    outreach.follow_up_due = None
                    report.closed += 1
                    continue
                template = FOLLOW_UP_TEXTS[count]
            else:
                template = RECHECK_TEXTS[outreach.state]
                outreach.follow_up_due = None  # one check-in; a reply restarts the flow
            text = self._text(template, outreach)
            if text is None:
                report.skipped += 1
                continue
            self.db.add(
                Message(
                    outreach_id=outreach.id,
                    direction=MessageDirection.OUTBOUND,
                    content=text,
                    status=MessageStatus.APPROVED,
                    review_status=ReviewStatus.PENDING_REVIEW,
                )
            )
            report.queued += 1
        self.db.commit()
        return report
