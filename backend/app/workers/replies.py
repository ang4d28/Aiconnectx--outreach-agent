"""Reply poller and reply state machine.

Polls each channel's read_new_replies every POLL_INTERVAL_MINUTES inside the
send window, matches each reply to its outreach, classifies it with the LLM and
applies the outcome. Every auto-reply is queued as an outbound message so it
goes through the hold window and all send-queue guardrails, on the channel the
thread started on. Answers to questions are drafted only and wait for SEND.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.channels.base import IncomingReply
from app.channels.linkedin import LinkedInError
from app.config import settings
from app.db import SessionLocal, repository
from app.db.models import (
    AttentionItem,
    AttentionType,
    ChannelType,
    Company,
    Contact,
    ContactRole,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    ReviewStatus,
)
from app.db.schemas import SettingCreate, SettingUpdate
from app.llm.prompts import ReplyClassificationResult, classify_reply_prompt
from app.workers.review import (
    CHANNEL_LABELS,
    Notify,
    format_preview,
    send_alert,
    whatsapp_notify,
)
from app.workers.send_queue import (
    DNC_KEY,
    EMAIL_DISABLED_KEY,
    as_utc,
    in_send_window,
    is_truthy,
    parse_dnc,
    thread_key,
    utc_now,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_MINUTES = 30
FIRST_POLL_LOOKBACK = timedelta(days=1)
NOT_NOW_DAYS = 30
FUTURE_POOL_DAYS = 60
MAX_CONNECT_CHECKS = 10
CURSOR_PREFIX = "replies_cursor:"
SEEN_PREFIX = "replies_seen:"
PENDING_STATUSES = (MessageStatus.DRAFTED, MessageStatus.APPROVED)


class AnswerDraft(BaseModel):
    message: str = Field(..., max_length=600)


@dataclass
class ReplyReport:
    handled: int = 0
    ignored: int = 0
    errors: int = 0
    connected: int = 0
    stopped: str | None = None
    intents: dict[str, int] = field(default_factory=dict)


# ─── Text ──────────────────────────────────────────────────


def first_name(name: str | None) -> str:
    return (name or "").split()[0] if (name or "").strip() else "there"


def tpo_text(contact: Contact) -> str:
    return (
        f"Thanks {first_name(contact.name)}, great to hear! Our Training & Placement Officer "
        f"{settings.tpo_name} will coordinate next steps. You can reach them at "
        f"{settings.tpo_phone} or {settings.tpo_email}."
    )


def mou_email_text(contact: Contact, company: str) -> str:
    return (
        f"Dear {first_name(contact.name)},\n\n"
        f"Thank you for your interest in partnering with AI ConnectX. We would like to propose "
        f"a Memorandum of Understanding between {company} and AI ConnectX for structured "
        f"campus hiring of our B.Tech AI/ML students: priority access to shortlisted "
        f"candidates, coordinated interview drives and internship pipelines.\n\n"
        f"Our Training & Placement Officer {settings.tpo_name} ({settings.tpo_phone}, "
        f"{settings.tpo_email}) will share the draft MoU and agree the next steps with you.\n\n"
        f"Best regards,\nAI ConnectX"
    )


def no_opportunity_text(contact: Contact, company: str) -> str:
    return (
        f"Thanks for letting me know, {first_name(contact.name)}. Even without open roles, "
        f"an MoU between {company} and AI ConnectX would give you first access to our AI/ML "
        f"students when positions open up. Could you share a phone number and email so we "
        f"can send the details?"
    )


def referral_thanks_text(contact: Contact, referral: str) -> str:
    return (
        f"Thank you, {first_name(contact.name)}! I'll reach out to {referral}. "
        f"Appreciate you pointing me in the right direction."
    )


def ask_referral_text(contact: Contact) -> str:
    return (
        f"Thanks {first_name(contact.name)}, and sorry for the mix-up. Could you point me to "
        f"the person who handles campus or early-career hiring at your company?"
    )


def first_dm_text(contact: Contact, company: str) -> str:
    return (
        f"Thanks for connecting, {first_name(contact.name)}! AI ConnectX works with companies "
        f"like {company} to hire B.Tech AI/ML students, with an MoU for structured campus hiring. "
        f"Would you be open to a short call to see if it fits your hiring plans?"
    )


def not_now_text(contact: Contact) -> str:
    return (
        f"Understood, {first_name(contact.name)}. I'll check back in about a month. "
        f"Thanks for getting back to me."
    )


def answer_prompt(question: str, person: str, company: str) -> str:
    return f"""Draft a short reply to a question from {person} at {company}, who received our outreach
from AI ConnectX (we connect companies with B.Tech AI/ML students and offer MoUs for structured
campus hiring).

THEIR MESSAGE:
{question}

REQUIREMENTS:
- Answer the question directly and honestly; do not invent facts, numbers or commitments.
- If a detail is unknown, say our Training & Placement Officer will confirm it.
- Under 80 words, professional and warm, one clear next step."""


async def classify_reply(text: str, llm: Any) -> ReplyClassificationResult:
    try:
        return await llm.complete_json(classify_reply_prompt(text), ReplyClassificationResult)
    except Exception as exc:
        logger.error(f"Reply classification failed: {exc}")
        return ReplyClassificationResult(intent="other")


# ─── Worker ────────────────────────────────────────────────


class ReplyWorker:
    def __init__(
        self,
        db: Any = None,
        channels: dict[ChannelType, Any] | None = None,
        llm: Any = None,
        notify: Notify = whatsapp_notify,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.db = db or SessionLocal()
        self._channels = channels
        self._llm = llm
        self.notify = notify
        self.clock = clock

    @property
    def channels(self) -> dict[ChannelType, Any]:
        if self._channels is None:
            from app.channels import linkedin, outlook

            self._channels = {
                ChannelType.LINKEDIN: linkedin.get_client(),
                ChannelType.EMAIL: outlook.get_client(),
            }
        return self._channels

    @property
    def llm(self) -> Any:
        if self._llm is None:
            from app.llm.client import llm_client

            self._llm = llm_client
        return self._llm

    # ─── Storage helpers (overridden in tests) ─────────────

    def _setting(self, key: str) -> str | None:
        setting = repository.get_setting(self.db, key)
        return setting.value if setting else None

    def _set_setting(self, key: str, value: str) -> None:
        existing = repository.get_setting(self.db, key)
        if existing:
            repository.update_setting(self.db, existing, SettingUpdate(value=value))
        else:
            repository.create_setting(self.db, SettingCreate(key=key, value=value))

    def _get(self, model: Any, key: Any) -> Any:
        return self.db.get(model, key) if key is not None else None

    def _outreach_for_sender(self, channel: ChannelType, sender: str | None) -> Outreach | None:
        if not sender:
            return None
        if channel == ChannelType.LINKEDIN:
            column = Contact.linkedin_url
            value = sender.strip().rstrip("/")
        else:
            column = Contact.email
            value = sender.strip().lower()
        stmt = (
            select(Outreach)
            .join(Contact, Contact.id == Outreach.contact_id)
            .where(column == value, Outreach.channel == channel)
            .order_by(Outreach.id.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def _pending_outbound(self, outreach_id: int) -> list[Message]:
        stmt = select(Message).where(
            Message.outreach_id == outreach_id,
            Message.direction == MessageDirection.OUTBOUND,
            Message.status.in_(PENDING_STATUSES),
        )
        return list(self.db.scalars(stmt))

    def _sibling_pending(self, contact_id: int, outreach_id: int) -> list[Message]:
        """Unsent messages on the contact's other channels (e.g. a cold email drafted next to a LinkedIn DM)."""
        stmt = (
            select(Message)
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(
                Outreach.contact_id == contact_id,
                Message.outreach_id != outreach_id,
                Message.direction == MessageDirection.OUTBOUND,
                Message.status.in_(PENDING_STATUSES),
            )
        )
        return list(self.db.scalars(stmt))

    def _email_outreach(self, contact_id: int) -> Outreach | None:
        stmt = (
            select(Outreach)
            .where(Outreach.contact_id == contact_id, Outreach.channel == ChannelType.EMAIL)
            .order_by(Outreach.id.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def _last_sent_text(self, outreach_id: int) -> str | None:
        stmt = (
            select(Message)
            .where(
                Message.outreach_id == outreach_id,
                Message.direction == MessageDirection.OUTBOUND,
                Message.status == MessageStatus.SENT,
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        message = self.db.scalar(stmt)
        return (message.edited_text or message.content) if message else None

    # ─── Polling ───────────────────────────────────────────

    async def run_once(self, check_connections: bool = True) -> ReplyReport:
        report = ReplyReport()
        now = self.clock()
        if not in_send_window(now):
            report.stopped = "outside_window"
            return report
        for channel, client in self.channels.items():
            if getattr(client, "available", True) is False:
                continue  # disabled after a security check; the owner was alerted
            if channel == ChannelType.EMAIL and is_truthy(self._setting(EMAIL_DISABLED_KEY)):
                continue  # Outlook disabled after repeated failures; owner alerted
            await self._poll(channel, client, now, report)
        linkedin = self.channels.get(ChannelType.LINKEDIN)
        if check_connections and linkedin is not None and getattr(linkedin, "available", True) is not False:
            report.connected = await self.check_connections(linkedin)
        return report

    def _pending_connects(self) -> list[Outreach]:
        stmt = (
            select(Outreach)
            .where(
                Outreach.channel == ChannelType.LINKEDIN,
                Outreach.state == OutreachState.CONNECT_SENT,
            )
            .order_by(Outreach.updated_at)
            .limit(MAX_CONNECT_CHECKS)
        )
        return list(self.db.scalars(stmt))

    async def check_connections(self, linkedin: Any) -> int:
        """Accepted connection requests become CONNECTED with the first DM queued for review."""
        connected = 0
        for outreach in self._pending_connects():
            contact = self._get(Contact, outreach.contact_id)
            outreach.updated_at = self.clock()  # rotate: least recently checked go first
            if contact is None or not contact.linkedin_url:
                continue
            try:
                accepted = await linkedin.is_connected(contact.linkedin_url)
            except LinkedInError as exc:
                logger.warning(f"Connection check stopped: {exc}")
                break
            except Exception as exc:
                logger.error(f"Connection check failed for outreach {outreach.id}: {exc}")
                continue
            if accepted:
                outreach.state = OutreachState.CONNECTED
                company = self._get(Company, contact.company_id)
                self._queue(outreach, first_dm_text(contact, company.name if company else "your team"))
                connected += 1
        self.db.commit()
        return connected

    async def _poll(self, channel: ChannelType, client: Any, now: datetime, report: ReplyReport) -> None:
        cursor = self._setting(f"{CURSOR_PREFIX}{channel.value}")
        since = datetime.fromisoformat(cursor) if cursor else as_utc(now) - FIRST_POLL_LOOKBACK
        seen = set(json.loads(self._setting(f"{SEEN_PREFIX}{channel.value}") or "[]"))
        try:
            replies = await client.read_new_replies(since)
        except NotImplementedError:
            return
        except LinkedInError as exc:
            # A block already disabled the channel, paused sending and alerted the owner.
            logger.warning(f"Reading {channel.value} replies stopped: {exc}")
            return
        except Exception as exc:
            logger.error(f"Reading {channel.value} replies failed: {exc}")
            self._attention(AttentionType.CHANNEL_ERROR, {"channel": channel.value, "error": str(exc)[:500]})
            self.db.commit()
            report.errors += 1
            return

        for reply in replies:
            if reply.message_id in seen:
                continue
            try:
                intent = await self.handle(channel, reply)
            except Exception as exc:
                logger.error(f"Handling reply {reply.message_id} failed: {exc}")
                self.db.rollback()
                self._attention(
                    AttentionType.NEEDS_MANUAL,
                    {"channel": channel.value, "reason": "Reply handling failed",
                     "sender": reply.sender, "text": reply.text[:1000]},
                )
                self.db.commit()
                report.errors += 1
                continue
            if intent is None:
                report.ignored += 1
            else:
                report.handled += 1
                report.intents[intent] = report.intents.get(intent, 0) + 1

        # Next poll starts here; ids from this batch guard the overlap at the boundary.
        self._set_setting(f"{CURSOR_PREFIX}{channel.value}", as_utc(now).isoformat())
        self._set_setting(f"{SEEN_PREFIX}{channel.value}", json.dumps([r.message_id for r in replies]))

    def _match(self, channel: ChannelType, reply: IncomingReply) -> Outreach | None:
        if reply.thread_id:
            outreach_id = self._setting(thread_key(channel, reply.thread_id))
            if outreach_id:
                return self._get(Outreach, int(outreach_id))
        return self._outreach_for_sender(channel, reply.sender)

    # ─── Reply handling ────────────────────────────────────

    async def handle(self, channel: ChannelType, reply: IncomingReply) -> str | None:
        outreach = self._match(channel, reply)
        if outreach is None:
            return None
        contact = self._get(Contact, outreach.contact_id)
        if contact is None:
            return None
        self._store_inbound(outreach, reply)
        if contact.is_unsubscribed or outreach.state == OutreachState.UNSUBSCRIBED:
            self.db.commit()
            return "ignored_unsubscribed"

        # A reply stops any follow-up or draft still waiting to go out.
        # The same applies to drafts waiting on the person's other channels.
        for message in self._pending_outbound(outreach.id) + self._sibling_pending(contact.id, outreach.id):
            message.status = MessageStatus.REJECTED
            message.review_status = ReviewStatus.CANCELLED
        outreach.follow_up_due = None

        result = await classify_reply(reply.text, self.llm)
        if result.email and not contact.email:
            contact.email = result.email.strip().lower()
        company = self._get(Company, contact.company_id)
        company_name = company.name if company else "your company"
        handler = getattr(self, f"_on_{result.intent}", self._on_other)
        await handler(outreach, contact, company_name, reply, result)
        self.db.commit()
        return result.intent

    def _store_inbound(self, outreach: Outreach, reply: IncomingReply) -> None:
        received = self.clock()
        if reply.received_at:
            try:
                received = datetime.fromisoformat(reply.received_at.replace("Z", "+00:00"))
            except ValueError:
                pass
        self.db.add(
            Message(
                outreach_id=outreach.id,
                direction=MessageDirection.INBOUND,
                content=reply.text,
                status=MessageStatus.SENT,
                review_status=ReviewStatus.APPROVED,
                sent_at=received,
            )
        )

    def _queue(self, outreach: Outreach, text: str, approved: bool = True) -> Message:
        """Outbound message for the send queue. Approved ones go through the hold window."""
        message = Message(
            direction=MessageDirection.OUTBOUND,
            content=text,
            status=MessageStatus.APPROVED if approved else MessageStatus.DRAFTED,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        if isinstance(outreach, Outreach) and outreach.id is None:
            message.outreach = outreach
        else:
            message.outreach_id = outreach.id
        self.db.add(message)
        return message

    def _attention(self, kind: AttentionType, payload: dict) -> AttentionItem:
        item = AttentionItem(type=kind, payload=payload)
        self.db.add(item)
        return item

    async def _on_interested(self, outreach, contact, company, reply, result) -> None:
        outreach.state = OutreachState.INTERESTED
        self._queue(outreach, tpo_text(contact))
        mou_queued = False
        if contact.email:
            if outreach.channel == ChannelType.EMAIL:
                email_outreach = outreach
            else:
                email_outreach = self._email_outreach(contact.id)
                if email_outreach is None:
                    email_outreach = Outreach(
                        contact_id=contact.id,
                        post_id=outreach.post_id,
                        channel=ChannelType.EMAIL,
                        follow_up_count=0,
                    )
                    self.db.add(email_outreach)
                email_outreach.state = OutreachState.INTERESTED
                email_outreach.follow_up_due = None
            self._queue(email_outreach, mou_email_text(contact, company))
            mou_queued = True

        last_sent = self._last_sent_text(outreach.id)
        summary = (
            f"Interested: {contact.name} ({company}) on {CHANNEL_LABELS[outreach.channel]}\n"
            + (f'We said: "{last_sent[:300]}"\n' if last_sent else "")
            + f'They said: "{reply.text[:500]}"\n'
            + "TPO details reply"
            + (" and MoU email" if mou_queued else "")
            + " queued for review."
        )
        self._attention(
            AttentionType.INTERESTED,
            {"outreach_id": outreach.id, "contact_id": contact.id, "summary": summary,
             "phone": result.phone, "email": result.email},
        )
        await send_alert(self.db, self.notify, summary)

    async def _on_no_opportunity(self, outreach, contact, company, reply, result) -> None:
        outreach.state = OutreachState.FUTURE_POOL
        outreach.follow_up_due = as_utc(self.clock()) + timedelta(days=FUTURE_POOL_DAYS)
        self._queue(outreach, no_opportunity_text(contact, company))

    async def _on_wrong_person(self, outreach, contact, company, reply, result) -> None:
        outreach.state = OutreachState.WRONG_PERSON
        referral = (result.referral_name or "").strip()
        if not referral:
            self._queue(outreach, ask_referral_text(contact))
            return
        self._queue(outreach, referral_thanks_text(contact, referral))
        new_contact = Contact(
            company_id=contact.company_id, name=referral, role=ContactRole.OTHER,
            is_unsubscribed=False,
        )
        self.db.add(new_contact)
        self.db.add(
            Outreach(
                contact=new_contact,
                post_id=outreach.post_id,
                channel=outreach.channel,
                state=OutreachState.TARGETED,
                follow_up_count=0,
            )
        )
        self._attention(
            AttentionType.NEEDS_MANUAL,
            {"outreach_id": outreach.id, "referral_name": referral, "company": company,
             "reason": f"Referred to {referral}: add their LinkedIn profile or email"},
        )

    async def _on_not_now(self, outreach, contact, company, reply, result) -> None:
        outreach.state = OutreachState.NOT_NOW
        outreach.follow_up_due = as_utc(self.clock()) + timedelta(days=NOT_NOW_DAYS)
        self._queue(outreach, not_now_text(contact))

    async def _on_question(self, outreach, contact, company, reply, result) -> None:
        outreach.state = OutreachState.REPLIED
        try:
            draft = await self.llm.complete_json(
                answer_prompt(reply.text, contact.name, company), AnswerDraft
            )
            answer = draft.message.strip()
        except Exception as exc:
            logger.error(f"Answer drafting failed: {exc}")
            await self._on_other(outreach, contact, company, reply, result)
            return
        message = self._queue(outreach, answer, approved=False)
        self.db.flush()
        self._attention(
            AttentionType.QUESTION,
            {"message_id": message.id, "outreach_id": outreach.id,
             "question": reply.text[:1000], "draft": answer},
        )
        preview = format_preview(message.id, outreach.channel, contact.name, company, answer, None)
        await send_alert(
            self.db, self.notify,
            f'Question from {contact.name} ({company}): "{reply.text[:500]}"\n\n{preview}',
            message.id,
        )

    async def _on_unsubscribe(self, outreach, contact, company, reply, result) -> None:
        outreach.state = OutreachState.UNSUBSCRIBED
        contact.is_unsubscribed = True
        dnc = parse_dnc(self._setting(DNC_KEY))
        for entry in (contact.email, contact.linkedin_url):
            if entry:
                dnc.add(entry.strip().lower())
        self._set_setting(DNC_KEY, "\n".join(sorted(dnc)))

    async def _on_other(self, outreach, contact, company, reply, result) -> None:
        outreach.state = OutreachState.REPLIED
        self._attention(
            AttentionType.NEEDS_MANUAL,
            {"outreach_id": outreach.id, "reason": "Reply needs a manual answer",
             "text": reply.text[:1000]},
        )
        await send_alert(
            self.db, self.notify,
            f'Needs attention: {contact.name} ({company}) replied: "{reply.text[:500]}"',
        )
