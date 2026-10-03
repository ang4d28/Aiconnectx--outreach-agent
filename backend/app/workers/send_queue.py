from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, repository
from app.db.models import (
    AttentionItem,
    AttentionType,
    ChannelType,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    ReviewStatus,
)
from app.db.schemas import SettingCreate, SettingUpdate

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
WARMUP_EMAIL_CAP = 20
FULL_EMAIL_CAP = 40
WARMUP_DAYS = 14
MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 240
MIN_LINKEDIN_REPLY_DELAY_SECONDS = 180
MAX_LINKEDIN_REPLY_DELAY_SECONDS = 720
FOLLOW_UP_DAYS = (4, 10)
CLOSE_AFTER_LAST_FOLLOW_UP_DAYS = 4
PAUSE_KEY = "global_pause"
DNC_KEY = "do_not_contact"
EMAIL_DISABLED_KEY = "outlook_disabled"
OUTREACH_THREAD_PREFIX = "outreach_thread:"
CONVERSATION_PREFIX = "email_thread:"
# Messages to leads who already replied: sent in-thread, no state transition.
REPLY_STATES = {
    OutreachState.REPLIED,
    OutreachState.INTERESTED,
    OutreachState.NO_OPPORTUNITY,
    OutreachState.WRONG_PERSON,
    OutreachState.NOT_NOW,
    OutreachState.FUTURE_POOL,
}
SENDABLE_STATES = {OutreachState.DRAFTED, OutreachState.CONTACTED} | REPLY_STATES
LINKEDIN_SENDABLE_STATES = {
    OutreachState.DRAFTED,
    OutreachState.CONNECTED,
    OutreachState.DM_SENT,
} | REPLY_STATES
# Messages in these states continue an existing conversation, so the 14-day
# one-touch rule does not apply to them.
FOLLOW_UP_STATES = {
    OutreachState.CONTACTED,
    OutreachState.CONNECTED,
    OutreachState.DM_SENT,
} | REPLY_STATES
CONNECTION_NOTE_MAX = 200
CONNECT_PENDING_DAYS = 14


@dataclass
class SendReport:
    sent: int = 0
    dry_run: int = 0
    skipped: int = 0
    blocked: int = 0
    failed: int = 0
    held: int = 0
    waiting: int = 0
    needs_manual: int = 0
    closed: int = 0
    stopped: str | None = None

    @property
    def used(self) -> int:
        return self.sent + self.dry_run


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def in_send_window(now: datetime) -> bool:
    local = as_utc(now).astimezone(IST).time()
    start = parse_hhmm(settings.send_window_start)
    end = parse_hhmm(settings.send_window_end)
    return start <= local < end


def ist_day_start(now: datetime) -> datetime:
    local = as_utc(now).astimezone(IST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def daily_cap(channel: ChannelType, first_send_at: datetime | None, now: datetime) -> int:
    """Same ramp per channel, counted separately: 20/day while warming up, then 40."""
    if first_send_at is None or as_utc(now) - as_utc(first_send_at) < timedelta(days=WARMUP_DAYS):
        return WARMUP_EMAIL_CAP
    return FULL_EMAIL_CAP


def email_daily_cap(first_send_at: datetime | None, now: datetime) -> int:
    return daily_cap(ChannelType.EMAIL, first_send_at, now)


def touched_recently(last_touch_at: datetime | None, now: datetime) -> bool:
    if last_touch_at is None:
        return False
    interval = timedelta(days=settings.min_touch_interval_days)
    return as_utc(now) - as_utc(last_touch_at) < interval


def is_truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_dnc(raw: str | None) -> set[str]:
    entries = (raw or "").replace("\n", ",").split(",")
    return {entry.strip().lower().lstrip("@") for entry in entries if entry.strip()}


def on_dnc_list(email: str, dnc: set[str]) -> bool:
    address = email.strip().lower()
    domain = address.rsplit("@", 1)[-1]
    return address in dnc or domain in dnc


def on_dnc(contact: Contact, dnc: set[str]) -> bool:
    if contact.email and on_dnc_list(contact.email, dnc):
        return True
    profile = (contact.linkedin_url or "").strip().lower().rstrip("/")
    return bool(profile) and profile in {entry.rstrip("/") for entry in dnc}


def route_channel(contact: Contact, preferred: ChannelType | None) -> ChannelType | None:
    """Keep the drafted channel when the lead is reachable on it, else fall back."""
    reachable = []
    if contact.linkedin_url:
        reachable.append(ChannelType.LINKEDIN)
    if contact.email:
        reachable.append(ChannelType.EMAIL)
    if preferred in reachable:
        return preferred
    return reachable[0] if reachable else None


def connect_expired(sent_at: datetime | None, now: datetime) -> bool:
    if sent_at is None:
        return False
    return as_utc(now) - as_utc(sent_at) >= timedelta(days=CONNECT_PENDING_DAYS)


def sendable_states(channel: ChannelType) -> set[OutreachState]:
    return LINKEDIN_SENDABLE_STATES if channel == ChannelType.LINKEDIN else SENDABLE_STATES


def pick_delay(rng: random.Random) -> float:
    return rng.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)


def pick_linkedin_reply_delay(rng: random.Random) -> float:
    return rng.uniform(MIN_LINKEDIN_REPLY_DELAY_SECONDS, MAX_LINKEDIN_REPLY_DELAY_SECONDS)


def thread_key(channel: ChannelType, thread_id: str) -> str:
    """Setting key mapping a channel thread back to its outreach id."""
    return f"{channel.value}_thread:{thread_id}"


def build_subject(company_name: str | None) -> str:
    if company_name:
        return f"AI/ML talent partnership with AI ConnectX x {company_name}"
    return "AI/ML talent partnership with AI ConnectX"


def build_mou_subject(company_name: str | None) -> str:
    return f"MoU proposal: AI ConnectX x {company_name or 'your team'}"


def next_follow_up(base: datetime, follow_up_count: int) -> datetime | None:
    if follow_up_count >= len(FOLLOW_UP_DAYS):
        return None
    return as_utc(base) + timedelta(days=FOLLOW_UP_DAYS[follow_up_count])


class SendQueue:
    def __init__(
        self,
        db: Session | None = None,
        client: Any = None,
        dry_run: bool | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utc_now,
        rng: random.Random | None = None,
        linkedin: Any = None,
        review: Any = None,
        reporter: Any = None,
    ):
        self.db = db or SessionLocal()
        self._client = client
        self._linkedin = linkedin
        self._review = review
        self._reporter = reporter
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()

    @property
    def client(self) -> Any:
        if self._client is None:
            from app.channels import outlook

            self._client = outlook.get_client()
        return self._client

    @property
    def linkedin(self) -> Any:
        if self._linkedin is None:
            from app.channels import linkedin

            self._linkedin = linkedin.get_client()
        return self._linkedin

    @property
    def review(self) -> Any:
        if self._review is None:
            from app.workers.review import ReviewService

            self._review = ReviewService(self.db, clock=self.clock)
        return self._review

    @property
    def reporter(self) -> Any:
        if self._reporter is None:
            from app.workers.reports import SendReporter

            self._reporter = SendReporter(self.db, clock=self.clock)
        return self._reporter

    def _setting(self, key: str) -> str | None:
        setting = repository.get_setting(self.db, key)
        return setting.value if setting else None

    def _set_setting(self, key: str, value: str) -> None:
        existing = repository.get_setting(self.db, key)
        if existing:
            repository.update_setting(self.db, existing, SettingUpdate(value=value))
        else:
            repository.create_setting(self.db, SettingCreate(key=key, value=value))

    def _sent_filter(self, channel: ChannelType) -> list[Any]:
        return [
            Outreach.channel == channel,
            Message.status == MessageStatus.SENT,
            Message.direction == MessageDirection.OUTBOUND,
        ]

    def _sent_today(self, now: datetime, channel: ChannelType) -> int:
        stmt = (
            select(func.count(Message.id))
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(*self._sent_filter(channel), Message.sent_at >= ist_day_start(now))
        )
        return self.db.scalar(stmt) or 0

    def _first_send_at(self, channel: ChannelType) -> datetime | None:
        stmt = (
            select(func.min(Message.sent_at))
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(*self._sent_filter(channel))
        )
        return self.db.scalar(stmt)

    def _approved_messages(self) -> list[Message]:
        stmt = (
            select(Message)
            .where(
                Message.status == MessageStatus.APPROVED,
                Message.direction == MessageDirection.OUTBOUND,
                Message.review_status != ReviewStatus.CANCELLED,
            )
            .order_by(Message.id)
        )
        return list(self.db.scalars(stmt))

    def _pending_connects(self) -> list[Outreach]:
        stmt = select(Outreach).where(
            Outreach.channel == ChannelType.LINKEDIN,
            Outreach.state == OutreachState.CONNECT_SENT,
        )
        return list(self.db.scalars(stmt))

    def _company_unsubscribed(self, company_id: int) -> bool:
        stmt = (
            select(Contact.id)
            .where(Contact.company_id == company_id, Contact.is_unsubscribed.is_(True))
            .limit(1)
        )
        return self.db.scalar(stmt) is not None

    def _thread_for(self, outreach_id: int) -> str | None:
        return self._setting(f"{OUTREACH_THREAD_PREFIX}{outreach_id}")

    def _save_thread(
        self, outreach_id: int, thread_id: str, channel: ChannelType = ChannelType.EMAIL
    ) -> None:
        self._set_setting(f"{OUTREACH_THREAD_PREFIX}{outreach_id}", thread_id)
        self._set_setting(thread_key(channel, thread_id), str(outreach_id))

    def _stop_reason(self, now: datetime) -> str | None:
        if is_truthy(self._setting(PAUSE_KEY)):
            return "paused"
        if not in_send_window(now):
            return "outside_window"
        return None

    def _capacity(self, now: datetime, channel: ChannelType) -> int:
        cap = daily_cap(channel, self._first_send_at(channel), now)
        if channel == ChannelType.EMAIL:
            cap = min(cap, settings.email_daily_cap)  # EMAIL_DAILY_CAP caps the 20 > 40 ramp
        remaining = max(0, cap - self._sent_today(now, channel))
        if channel == ChannelType.LINKEDIN:
            # LINKEDIN_DAILY_CAP is shared with comments and other LinkedIn actions.
            shared = getattr(self.linkedin, "remaining_today", None)
            left = shared() if callable(shared) else None
            if isinstance(left, int):
                remaining = min(remaining, left)
        return remaining

    def _is_blocked(
        self, outreach: Outreach, contact: Contact, channel: ChannelType, dnc: set[str]
    ) -> bool:
        if outreach.state not in sendable_states(channel) or contact.is_unsubscribed:
            return True
        if on_dnc(contact, dnc):
            return True
        return self._company_unsubscribed(contact.company_id)

    def _reject(self, message: Message) -> None:
        message.status = MessageStatus.REJECTED
        self.db.commit()

    def _mark_failed(self, message: Message) -> None:
        message.status = MessageStatus.FAILED
        self.db.commit()

    def _needs_manual(self, message: Message, outreach: Outreach | None, reason: str) -> None:
        self.db.add(
            AttentionItem(
                type=AttentionType.NEEDS_MANUAL,
                payload={
                    "message_id": message.id,
                    "outreach_id": outreach.id if outreach else None,
                    "reason": reason,
                },
            )
        )
        message.status = MessageStatus.FAILED
        self.db.commit()

    # ─── Hold window ───────────────────────────────────────

    async def _start_hold(self, message: Message) -> str:
        outreach = repository.get_outreach(self.db, message.outreach_id)
        contact = repository.get_contact(self.db, outreach.contact_id) if outreach else None
        channel = route_channel(contact, outreach.channel) if contact else None
        if channel is None:
            self._needs_manual(message, outreach, "Lead has no LinkedIn profile or email")
            return "needs_manual"
        outreach.channel = channel
        company = repository.get_company(self.db, contact.company_id)
        await self.review.start_hold(
            message, channel, contact.name, company.name if company else "unknown"
        )
        return "held"

    def close_stale_connects(self, now: datetime) -> int:
        closed = 0
        for outreach in self._pending_connects():
            if connect_expired(outreach.sent_at, now):
                outreach.state = OutreachState.CLOSED
                closed += 1
        if closed:
            self.db.commit()
        return closed

    # ─── Delivery ──────────────────────────────────────────

    def _mark_sent(
        self,
        message: Message,
        outreach: Outreach,
        contact: Contact,
        thread_id: str | None,
        now: datetime,
    ) -> None:
        message.status = MessageStatus.SENT
        message.sent_at = now
        contact.last_touch_at = now
        linkedin = outreach.channel == ChannelType.LINKEDIN
        if outreach.state in REPLY_STATES:
            pass  # conversation reply: the reply worker owns state and rechecks
        elif outreach.state in (OutreachState.CONTACTED, OutreachState.DM_SENT):
            outreach.follow_up_count = (outreach.follow_up_count or 0) + 1
            # After the last follow-up, the due date becomes the close date.
            outreach.follow_up_due = next_follow_up(
                outreach.sent_at or now, outreach.follow_up_count
            ) or as_utc(now) + timedelta(days=CLOSE_AFTER_LAST_FOLLOW_UP_DAYS)
        elif linkedin and outreach.state == OutreachState.DRAFTED:
            # Connection request: follow-ups only start after the first DM.
            outreach.state = OutreachState.CONNECT_SENT
            outreach.sent_at = now
            outreach.follow_up_due = None
        else:
            outreach.state = OutreachState.DM_SENT if linkedin else OutreachState.CONTACTED
            outreach.sent_at = now
            outreach.follow_up_count = 0
            outreach.follow_up_due = next_follow_up(now, 0)
        self.db.commit()
        if thread_id:
            self._save_thread(outreach.id, thread_id, outreach.channel)

    async def _send_via_channel(
        self, message: Message, outreach: Outreach, contact: Contact
    ) -> str | None:
        text = message.edited_text or message.content
        if outreach.state in REPLY_STATES:
            return await self._send_reply(outreach, contact, text)
        if outreach.channel == ChannelType.LINKEDIN:
            if outreach.state == OutreachState.DRAFTED:
                return await self.linkedin.send_connection_note(contact.linkedin_url, text)
            return await self.linkedin.send_dm(contact.linkedin_url, text)
        company = repository.get_company(self.db, contact.company_id)
        subject = build_subject(company.name if company else None)
        return await self.client.send_email(
            contact.email, subject, text, self._thread_for(outreach.id)
        )

    async def _send_reply(self, outreach: Outreach, contact: Contact, text: str) -> str | None:
        """Reply on the channel the thread started on, in-thread when known."""
        from app.channels.base import Thread

        thread_id = self._thread_for(outreach.id)
        if outreach.channel == ChannelType.LINKEDIN:
            return await self.linkedin.reply(
                Thread(recipient=contact.linkedin_url, thread_id=thread_id), text
            )
        company = repository.get_company(self.db, contact.company_id)
        company_name = company.name if company else None
        subject = (
            build_mou_subject(company_name)
            if outreach.state == OutreachState.INTERESTED and thread_id is None
            else build_subject(company_name)
        )
        return await self.client.reply(
            Thread(recipient=contact.email, subject=subject, thread_id=thread_id), text
        )

    async def _deliver(
        self, message: Message, outreach: Outreach, contact: Contact, delay: bool
    ) -> str:
        is_linkedin = outreach.channel == ChannelType.LINKEDIN
        if is_linkedin and getattr(self.linkedin, "available", True) is False:
            logger.info(f"LinkedIn not available yet for message {message.id}; left queued")
            return "skipped"
        linkedin_reply = is_linkedin and outreach.state in REPLY_STATES
        if delay or linkedin_reply:
            wait = pick_linkedin_reply_delay(self.rng) if linkedin_reply else pick_delay(self.rng)
            await self.sleep(wait)
            reason = self._stop_reason(self.clock())
            if reason:
                return reason
        from app.channels.linkedin import (
            LinkedInActionFailed,
            LinkedInBlocked,
            LinkedInUnavailable,
        )

        try:
            thread_id = await self._send_via_channel(message, outreach, contact)
        except NotImplementedError:
            logger.info(f"Channel not available yet for message {message.id}; left queued")
            return "skipped"
        except LinkedInBlocked:
            # Channel disabled, global pause set, owner alerted: stop the run, keep the message.
            return "paused"
        except LinkedInUnavailable as exc:
            logger.info(f"LinkedIn unavailable for message {message.id}: {exc}; left queued")
            return "skipped"
        except LinkedInActionFailed as exc:
            self._needs_manual(message, outreach, f"LinkedIn: {exc}")
            return "needs_manual"
        except Exception as exc:
            logger.error(f"Send failed for message {message.id}: {exc}")
            self._mark_failed(message)
            return "failed"
        self._mark_sent(message, outreach, contact, thread_id, self.clock())
        company = repository.get_company(self.db, contact.company_id)
        await self.reporter.record_send(
            message.id, outreach.channel, contact.name, company.name if company else "unknown"
        )
        return "sent"

    async def _process(
        self, message: Message, outreach: Outreach, contact: Contact, dnc: set[str], delay: bool
    ) -> str:
        channel = outreach.channel
        if self._is_blocked(outreach, contact, channel, dnc):
            self._reject(message)
            return "blocked"
        if (
            channel == ChannelType.LINKEDIN
            and outreach.state == OutreachState.DRAFTED
            and len(message.edited_text or message.content) > CONNECTION_NOTE_MAX
        ):
            self._needs_manual(message, outreach, "Connection note over 200 characters")
            return "needs_manual"
        if outreach.state not in FOLLOW_UP_STATES and touched_recently(
            contact.last_touch_at, self.clock()
        ):
            return "skipped"
        if message.review_status in (ReviewStatus.PENDING_REVIEW, ReviewStatus.EDITED):
            message.review_status = ReviewStatus.AUTO_RELEASED
            self.db.commit()
        if self.dry_run:
            logger.info(f"DRY_RUN: would send message {message.id} via {channel.value}")
            return "dry_run"
        return await self._deliver(message, outreach, contact, delay)

    async def run_once(self, start_holds: bool = True) -> SendReport:
        """start_holds=False only releases messages whose hold has passed (the 1-minute job)."""
        try:
            return await self._run(start_holds)
        finally:
            # Flush notices held back during a burst of sends.
            try:
                await self.reporter.flush()
            except Exception as exc:
                logger.error(f"Flushing send notices failed: {exc}")

    async def _run(self, start_holds: bool = True) -> SendReport:
        report = SendReport()
        now = self.clock()
        report.closed = self.close_stale_connects(now)

        # Start holds for newly approved messages; collect those whose hold has passed.
        ready: list[Message] = []
        for message in self._approved_messages():
            if message.review_status == ReviewStatus.CANCELLED:
                continue
            if message.hold_until is None:
                if start_holds:
                    outcome = await self._start_hold(message)
                    setattr(report, outcome, getattr(report, outcome) + 1)
            elif as_utc(message.hold_until) > now:
                report.waiting += 1
            else:
                ready.append(message)

        report.stopped = self._stop_reason(now)
        if report.stopped:
            return report

        capacity: dict[ChannelType, int] = {}
        used = {channel: 0 for channel in ChannelType}
        dnc = parse_dnc(self._setting(DNC_KEY))
        for message in ready:
            outreach = repository.get_outreach(self.db, message.outreach_id)
            contact = repository.get_contact(self.db, outreach.contact_id) if outreach else None
            if contact is None:
                report.skipped += 1
                continue
            channel = outreach.channel
            if channel == ChannelType.EMAIL and is_truthy(self._setting(EMAIL_DISABLED_KEY)):
                report.skipped += 1  # Outlook disabled after repeated failures; owner alerted
                continue
            if channel not in capacity:
                capacity[channel] = self._capacity(now, channel)
            if used[channel] >= capacity[channel]:
                report.stopped = "daily_cap"
                continue
            outcome = await self._process(message, outreach, contact, dnc, delay=report.sent > 0)
            if outcome in {"paused", "outside_window"}:
                report.stopped = outcome
                break
            setattr(report, outcome, getattr(report, outcome) + 1)
            if outcome in {"sent", "dry_run"}:
                used[channel] += 1
        return report
