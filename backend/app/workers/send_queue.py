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
    ChannelType,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
)
from app.db.schemas import SettingCreate, SettingUpdate

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
WARMUP_EMAIL_CAP = 20
FULL_EMAIL_CAP = 40
WARMUP_DAYS = 14
MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 240
FOLLOW_UP_DAYS = (4, 10)
PAUSE_KEY = "global_pause"
DNC_KEY = "do_not_contact"
OUTREACH_THREAD_PREFIX = "outreach_thread:"
CONVERSATION_PREFIX = "email_thread:"
SENDABLE_STATES = {OutreachState.DRAFTED, OutreachState.CONTACTED}


@dataclass
class SendReport:
    sent: int = 0
    dry_run: int = 0
    skipped: int = 0
    blocked: int = 0
    failed: int = 0
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


def email_daily_cap(first_send_at: datetime | None, now: datetime) -> int:
    if first_send_at is None or as_utc(now) - as_utc(first_send_at) < timedelta(days=WARMUP_DAYS):
        return WARMUP_EMAIL_CAP
    return FULL_EMAIL_CAP


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


def pick_delay(rng: random.Random) -> float:
    return rng.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)


def build_subject(company_name: str | None) -> str:
    if company_name:
        return f"AI/ML talent partnership with AI ConnectX x {company_name}"
    return "AI/ML talent partnership with AI ConnectX"


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
    ):
        self.db = db or SessionLocal()
        self._client = client
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

    def _setting(self, key: str) -> str | None:
        setting = repository.get_setting(self.db, key)
        return setting.value if setting else None

    def _set_setting(self, key: str, value: str) -> None:
        existing = repository.get_setting(self.db, key)
        if existing:
            repository.update_setting(self.db, existing, SettingUpdate(value=value))
        else:
            repository.create_setting(self.db, SettingCreate(key=key, value=value))

    def _sent_email_filter(self) -> list[Any]:
        return [
            Outreach.channel == ChannelType.EMAIL,
            Message.status == MessageStatus.SENT,
            Message.direction == MessageDirection.OUTBOUND,
        ]

    def _sent_today(self, now: datetime) -> int:
        stmt = (
            select(func.count(Message.id))
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(*self._sent_email_filter(), Message.sent_at >= ist_day_start(now))
        )
        return self.db.scalar(stmt) or 0

    def _first_send_at(self) -> datetime | None:
        stmt = (
            select(func.min(Message.sent_at))
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(*self._sent_email_filter())
        )
        return self.db.scalar(stmt)

    def _approved_email_messages(self) -> list[Message]:
        stmt = (
            select(Message)
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(
                Outreach.channel == ChannelType.EMAIL,
                Message.status == MessageStatus.APPROVED,
                Message.direction == MessageDirection.OUTBOUND,
            )
            .order_by(Message.id)
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

    def _save_thread(self, outreach_id: int, conversation_id: str) -> None:
        self._set_setting(f"{OUTREACH_THREAD_PREFIX}{outreach_id}", conversation_id)
        self._set_setting(f"{CONVERSATION_PREFIX}{conversation_id}", str(outreach_id))

    def _stop_reason(self, now: datetime) -> str | None:
        if is_truthy(self._setting(PAUSE_KEY)):
            return "paused"
        if not in_send_window(now):
            return "outside_window"
        return None

    def _capacity(self, now: datetime) -> int:
        cap = email_daily_cap(self._first_send_at(), now)
        return max(0, cap - self._sent_today(now))

    def _is_blocked(self, outreach: Outreach, contact: Contact, dnc: set[str]) -> bool:
        if outreach.state not in SENDABLE_STATES or contact.is_unsubscribed:
            return True
        if on_dnc_list(contact.email, dnc):
            return True
        return self._company_unsubscribed(contact.company_id)

    def _reject(self, message: Message) -> None:
        message.status = MessageStatus.REJECTED
        self.db.commit()

    def _mark_failed(self, message: Message) -> None:
        message.status = MessageStatus.FAILED
        self.db.commit()

    def _mark_sent(
        self,
        message: Message,
        outreach: Outreach,
        contact: Contact,
        conversation_id: str | None,
        now: datetime,
    ) -> None:
        message.status = MessageStatus.SENT
        message.sent_at = now
        contact.last_touch_at = now
        if outreach.state == OutreachState.CONTACTED:
            outreach.follow_up_count = (outreach.follow_up_count or 0) + 1
            outreach.follow_up_due = next_follow_up(
                outreach.sent_at or now, outreach.follow_up_count
            )
        else:
            outreach.state = OutreachState.CONTACTED
            outreach.sent_at = now
            outreach.follow_up_count = 0
            outreach.follow_up_due = next_follow_up(now, 0)
        self.db.commit()
        if conversation_id:
            self._save_thread(outreach.id, conversation_id)

    async def _deliver(
        self, message: Message, outreach: Outreach, contact: Contact, delay: bool
    ) -> str:
        if delay:
            await self.sleep(pick_delay(self.rng))
            reason = self._stop_reason(self.clock())
            if reason:
                return reason
        company = repository.get_company(self.db, contact.company_id)
        subject = build_subject(company.name if company else None)
        try:
            conversation_id = await self.client.send_email(
                contact.email, subject, message.content, self._thread_for(outreach.id)
            )
        except Exception as exc:
            logger.error(f"Email send failed for message {message.id}: {exc}")
            self._mark_failed(message)
            return "failed"
        self._mark_sent(message, outreach, contact, conversation_id, self.clock())
        return "sent"

    async def _process(self, message: Message, dnc: set[str], delay: bool) -> str:
        outreach = repository.get_outreach(self.db, message.outreach_id)
        contact = repository.get_contact(self.db, outreach.contact_id) if outreach else None
        if contact is None or not contact.email:
            return "skipped"
        if self._is_blocked(outreach, contact, dnc):
            self._reject(message)
            return "blocked"
        is_follow_up = outreach.state == OutreachState.CONTACTED
        if not is_follow_up and touched_recently(contact.last_touch_at, self.clock()):
            return "skipped"
        if self.dry_run:
            logger.info(f"DRY_RUN: would email {contact.email} (message {message.id})")
            return "dry_run"
        return await self._deliver(message, outreach, contact, delay)

    async def run_once(self) -> SendReport:
        report = SendReport()
        now = self.clock()
        report.stopped = self._stop_reason(now)
        if report.stopped:
            return report
        capacity = self._capacity(now)
        dnc = parse_dnc(self._setting(DNC_KEY))
        for message in self._approved_email_messages():
            if report.used >= capacity:
                report.stopped = "daily_cap"
                break
            outcome = await self._process(message, dnc, delay=report.sent > 0)
            if outcome in {"paused", "outside_window"}:
                report.stopped = outcome
                break
            setattr(report, outcome, getattr(report, outcome) + 1)
        return report
