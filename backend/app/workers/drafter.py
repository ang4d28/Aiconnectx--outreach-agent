from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, repository
from app.db.models import (
    AttentionItem,
    AttentionStatus,
    AttentionType,
    ChannelType,
    Contact,
    DraftVersion,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    Post,
    ReviewStatus,
)
from app.demo import DEMO_POST_MARKER
from app.db.schemas import MessageCreate, OutreachCreate, OutreachUpdate
from app.llm.client import llm_client
from app.llm.prompts import draft_message_prompt

logger = logging.getLogger(__name__)


FACTS_FILE = Path(__file__).resolve().parents[3] / "facts" / "ai_connectx.md"
MAX_SUBJECT_WORDS = 8
MIN_WORDS, MAX_WORDS = 50, 90  # what the prompt asks for
WORD_TOLERANCE = 0.2  # validation allows 20% either way, since models miscount
PLACEHOLDER_MARKERS = ("[", "]", "{{")
FIRST_PERSON_CLAIM = r"\b(?:I am|I'm|we are|we're|my name is)\b[^.!?\n]{0,80}?"


class DraftedMessage(BaseModel):
    message: str
    subject: str = ""


@dataclass
class Draft:
    subject: str
    body: str


class DrafterConfigError(RuntimeError):
    """The drafter cannot start: a required setting is missing."""


class RegenerateRefused(Exception):
    """The message cannot be regenerated (already sent, being released, or not a cold-outreach draft)."""


class DraftRejected(Exception):
    """The LLM draft still fails validation after one regeneration."""

    def __init__(self, problems: list[str], body: str):
        super().__init__("; ".join(problems))
        self.body = body


def load_facts(path: Path = FACTS_FILE) -> str:
    """The facts file without its HTML comments (those are notes for the owner)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def sender_info() -> dict[str, str]:
    return {"org": settings.org_name, "university": settings.university_name}


def require_sender_name() -> None:
    if not settings.sender_name.strip():
        raise DrafterConfigError(
            "SENDER_NAME is empty. Set SENDER_NAME (and SENDER_TITLE) in .env: every draft is signed with it."
        )


def signature() -> str:
    """Closing block built from the SENDER_* settings; the LLM never writes it."""
    lines = ["Regards,"]
    if settings.sender_name:
        lines += [settings.sender_name, ", ".join(p for p in (settings.sender_title, settings.org_name) if p)]
    else:
        lines.append(f"The {settings.org_name} team")
    if settings.sender_contact:
        lines.append(settings.sender_contact)
    return "\n".join(lines)


def compose(recipient_name: str | None, body: str) -> str:
    parts = (recipient_name or "").split()
    return f"Hi {parts[0] if parts else 'there'},\n\n{body.strip()}\n\n{signature()}"


def validate_draft(
    subject: str, body: str, target_info: dict[str, str], channel: ChannelType | None = None
) -> list[str]:
    """Reasons the draft must not be used (empty when it is clean). The length rule is only checked for email
    (pass channel): a LinkedIn connection note is capped at 200 characters, far below 50 words."""
    problems: list[str] = []
    text = f"{subject}\n{body}"
    if any(marker in text for marker in PLACEHOLDER_MARKERS) or "your name" in text.lower():
        problems.append("it contains a placeholder (square brackets, {{ or 'Your Name')")
    words = len(body.split())
    if channel == ChannelType.EMAIL and (
        words < MIN_WORDS * (1 - WORD_TOLERANCE) or words > MAX_WORDS * (1 + WORD_TOLERANCE)
    ):
        problems.append(f"the body has {words} words and must be {MIN_WORDS} to {MAX_WORDS}")
    title = (target_info.get("title") or "").strip()
    company = (target_info.get("company") or "").strip()
    title = "" if title.lower() == "n/a" else title
    company = "" if company.lower() == "n/a" else company
    if title and title.lower() == settings.sender_title.strip().lower():
        problems.append("the sender's title equals the recipient's title")
    if title and re.search(FIRST_PERSON_CLAIM + re.escape(title), body, re.IGNORECASE):
        problems.append("it describes the sender as holding the recipient's title")
    if company and re.search(FIRST_PERSON_CLAIM + r"\bat " + re.escape(company), body, re.IGNORECASE):
        problems.append("it describes the sender as working at the recipient's company")
    return problems


def pick_channels(contact: Contact, post_triggered: bool) -> list[ChannelType]:
    available: list[ChannelType] = []
    if contact.linkedin_url:
        available.append(ChannelType.LINKEDIN)
    if contact.email:
        available.append(ChannelType.EMAIL)
    if post_triggered:
        return available
    return available[:1]


def build_target_info(contact: Contact, company_name: str) -> dict[str, str]:
    return {
        "name": contact.name,
        "title": contact.title or "N/A",
        "company": company_name,
    }


def build_context(post: Post | None, company_size: str | None) -> dict[str, str]:
    post_context = "General outreach"
    if post is not None:
        roles = ", ".join(post.roles or [])
        post_context = roles or post.raw_text or post_context
    return {
        "post_context": post_context[:500],
        "company_size": company_size or "Unknown",
        "has_post": "no" if post is None else "yes",
    }


async def draft_message(
    channel: ChannelType, target_info: dict[str, str], context: dict[str, str], instruction: str = ""
) -> Draft:
    """Subject and body from the LLM (no greeting, no signature). A draft that fails validation is regenerated
    once; a second failure raises DraftRejected."""
    facts = load_facts()
    reason = ""
    for attempt in range(2):
        prompt = draft_message_prompt(
            channel.value,
            target_info,
            context,
            sender=sender_info(),
            facts=facts,
            retry_reason=reason,
            instruction=instruction,
        )
        result = await llm_client.complete_json(prompt, DraftedMessage)
        body = result.message.strip()
        subject = " ".join(result.subject.split()[:MAX_SUBJECT_WORDS])
        problems = validate_draft(subject, body, target_info, channel)
        if not problems:
            return Draft(subject=subject, body=body)
        reason = "; ".join(problems)
        logger.warning(f"Draft rejected ({reason}); {'regenerating' if attempt == 0 else 'giving up'}")
    raise DraftRejected(problems, body)


class Drafter:
    def __init__(self, db: Session | None = None):
        require_sender_name()
        self.db = db or SessionLocal()

    def _enriched_outreaches(self) -> list[Outreach]:
        stmt = select(Outreach).where(Outreach.state == OutreachState.ENRICHED)
        return list(self.db.scalars(stmt))

    def _outreach_for_channel(
        self, outreach: Outreach, channel: ChannelType, first: bool
    ) -> Outreach:
        if first:
            return repository.update_outreach(
                self.db, outreach, OutreachUpdate(channel=channel)
            )
        return repository.create_outreach(
            self.db,
            OutreachCreate(
                contact_id=outreach.contact_id,
                post_id=outreach.post_id,
                channel=channel,
                state=OutreachState.ENRICHED,
            ),
        )

    async def draft_outreach(self, outreach: Outreach) -> int:
        contact = repository.get_contact(self.db, outreach.contact_id)
        if contact is None or contact.is_unsubscribed:
            return 0
        channels = pick_channels(contact, outreach.post_id is not None)
        if not channels:
            logger.warning(f"Outreach {outreach.id} has no reachable channel")
            return 0
        company = repository.get_company(self.db, contact.company_id)
        post = repository.get_post(self.db, outreach.post_id) if outreach.post_id else None
        target = build_target_info(contact, company.name if company else "N/A")
        context = build_context(post, company.size if company else None)
        for index, channel in enumerate(channels):
            target_outreach = self._outreach_for_channel(outreach, channel, index == 0)
            await self._store_draft(target_outreach, channel, target, context)
        return len(channels)

    async def _store_draft(
        self,
        outreach: Outreach,
        channel: ChannelType,
        target: dict[str, str],
        context: dict[str, str],
    ) -> None:
        try:
            draft = await draft_message(channel, target, context)
        except DraftRejected as rejected:
            self._store_failed(outreach, rejected)
            return
        repository.create_message(
            self.db,
            MessageCreate(
                outreach_id=outreach.id,
                direction=MessageDirection.OUTBOUND,
                content=compose(target.get("name"), draft.body),
                status=MessageStatus.DRAFTED,
                subject=(draft.subject or None) if channel == ChannelType.EMAIL else None,
            ),
        )
        repository.update_outreach(
            self.db, outreach, OutreachUpdate(state=OutreachState.DRAFTED)
        )

    def _store_failed(self, outreach: Outreach, rejected: DraftRejected) -> None:
        """A draft that stays unclean is kept as FAILED (the queue never sends it) and the owner is told."""
        message = repository.create_message(
            self.db,
            MessageCreate(
                outreach_id=outreach.id,
                direction=MessageDirection.OUTBOUND,
                content=rejected.body,
                status=MessageStatus.FAILED,
            ),
        )
        self.db.add(
            AttentionItem(
                type=AttentionType.NEEDS_MANUAL,
                payload={
                    "message_id": message.id,
                    "outreach_id": outreach.id,
                    "reason": f"Draft failed validation twice: {rejected}. Write this message by hand.",
                },
            )
        )
        self.db.commit()
        # Out of ENRICHED so the next run does not draft (and fail) the same lead again.
        repository.update_outreach(self.db, outreach, OutreachUpdate(state=OutreachState.DRAFTED))
        logger.error(f"Draft for outreach {outreach.id} failed validation twice: {rejected}")

    async def run_once(self) -> int:
        drafted = 0
        for outreach in self._enriched_outreaches():
            try:
                drafted += await self.draft_outreach(outreach)
            except Exception as exc:
                logger.error(f"Drafting failed for outreach {outreach.id}: {exc}")
        return drafted


# ─── Regenerate one draft ──────────────────────────────────


def check_regenerable(db: Session, message: Message, now: datetime) -> Outreach:
    """The outreach of a draft that may be rewritten; raises RegenerateRefused for anything else."""
    from app.workers.review import OPEN_REVIEW
    from app.workers.send_queue import REPLY_STATES, as_utc

    if message.direction != MessageDirection.OUTBOUND:
        raise RegenerateRefused("Only outgoing drafts can be regenerated")
    if message.status not in (MessageStatus.DRAFTED, MessageStatus.APPROVED):
        raise RegenerateRefused(f"This message is already {message.status.value} and can no longer be changed")
    outreach = repository.get_outreach(db, message.outreach_id)
    if outreach is None:
        raise RegenerateRefused("The lead for this draft no longer exists")
    if outreach.state in REPLY_STATES:
        raise RegenerateRefused("Replies in a conversation are not regenerated; edit them by hand")
    if message.status == MessageStatus.APPROVED and (
        message.review_status not in OPEN_REVIEW
        or (message.hold_until is not None and as_utc(message.hold_until) <= now)
    ):
        raise RegenerateRefused("This message was released and is being sent")
    return outreach


async def regenerate_message(
    db: Session, message_id: int, instruction: str = "", user_id: int | None = None
) -> Message:
    """Rewrite an unsent cold-outreach draft with the current prompt, facts and sender settings.

    The old text goes to draft_versions. The review goes back to pending_review and, if the message already
    had a hold running, the hold and the WhatsApp preview restart. Raises LookupError (no such message),
    RegenerateRefused (sent or being sent), DraftRejected (still unclean after one retry) or the LLM error
    when every provider fails; the message is untouched in all of those cases."""
    from app.workers.review import ReviewService, review_context
    from app.workers.send_queue import utc_now

    require_sender_name()
    message = db.get(Message, message_id)
    if message is None:
        raise LookupError(f"Draft {message_id} not found")
    outreach = check_regenerable(db, message, utc_now())
    contact = repository.get_contact(db, outreach.contact_id)
    company = repository.get_company(db, contact.company_id) if contact else None
    if contact is None:
        raise RegenerateRefused("The contact for this draft no longer exists")
    post = repository.get_post(db, outreach.post_id) if outreach.post_id else None
    target = build_target_info(contact, company.name if company else "N/A")
    context = build_context(post, company.size if company else None)
    draft = await draft_message(outreach.channel, target, context, instruction)

    # The LLM call took a while: look again, with the row locked on Postgres, before writing.
    message = db.scalar(select(Message).where(Message.id == message_id).with_for_update())
    db.refresh(message)
    now = utc_now()
    check_regenerable(db, message, now)
    db.add(
        DraftVersion(
            draft_id=message.id,
            text=message.edited_text or message.content,
            subject=message.subject,
            created_by=user_id,
        )
    )
    message.content = compose(target.get("name"), draft.body)
    message.edited_text = None
    message.subject = (draft.subject or None) if outreach.channel == ChannelType.EMAIL else None
    message.review_status = ReviewStatus.PENDING_REVIEW
    message.reviewed_by = user_id
    message.reviewed_at = now
    message.regenerated_at = now
    if message.status == MessageStatus.APPROVED and message.hold_until is not None:
        channel, person, company_name = review_context(db, message)
        await ReviewService(db).start_hold(message, channel, person, company_name)  # commits
    else:
        db.commit()
    return message


async def restore_version(
    db: Session, message_id: int, version_id: int | None = None, user_id: int | None = None
) -> Message:
    """Put an earlier text back. The text it replaces is kept as a version, so a restore can be undone."""
    from app.workers.review import ReviewService, review_context
    from app.workers.send_queue import utc_now

    message = db.scalar(select(Message).where(Message.id == message_id).with_for_update())
    if message is None:
        raise LookupError(f"Draft {message_id} not found")
    now = utc_now()
    check_regenerable(db, message, now)
    stmt = select(DraftVersion).where(DraftVersion.draft_id == message_id)
    if version_id is not None:
        stmt = stmt.where(DraftVersion.id == version_id)
    version = db.scalars(stmt.order_by(DraftVersion.id.desc()).limit(1)).first()
    if version is None:
        raise LookupError("There is no previous version of this draft")
    db.add(
        DraftVersion(
            draft_id=message.id,
            text=message.edited_text or message.content,
            subject=message.subject,
            created_by=user_id,
        )
    )
    message.content = version.text
    message.edited_text = None
    message.subject = version.subject
    message.review_status = ReviewStatus.PENDING_REVIEW
    message.reviewed_by = user_id
    message.reviewed_at = now
    if message.status == MessageStatus.APPROVED and message.hold_until is not None:
        channel, person, company_name = review_context(db, message)
        await ReviewService(db).start_hold(message, channel, person, company_name)
    else:
        db.commit()
    return message


# ─── Force a new draft (CLI) ───────────────────────────────

DISCARDABLE = (MessageStatus.DRAFTED, MessageStatus.APPROVED, MessageStatus.FAILED)


@dataclass
class ForceResult:
    discarded: int = 0
    drafted: int = 0
    refused: list[tuple[int, str]] = field(default_factory=list)


def reset_refusal(db: Session, group: list[Outreach]) -> str | None:
    """Why this lead must not be reset (None when it is safe): anything sent, anything received."""
    ids = [o.id for o in group]
    if any(o.state not in (OutreachState.ENRICHED, OutreachState.DRAFTED) for o in group):
        return "the lead has moved past DRAFTED"
    sent = db.scalar(
        select(Message.id)
        .where(
            Message.outreach_id.in_(ids),
            (Message.direction == MessageDirection.INBOUND) | (Message.status == MessageStatus.SENT),
        )
        .limit(1)
    )
    return "a message was already sent or received" if sent is not None else None


def _dismiss_attention(db: Session, message_ids: set[int]) -> None:
    for item in db.scalars(select(AttentionItem).where(AttentionItem.status == AttentionStatus.OPEN)):
        if (item.payload or {}).get("message_id") in message_ids:
            item.status = AttentionStatus.DISMISSED


async def force_redraft(db: Session, outreach_ids: list[int] | None = None) -> ForceResult:
    """Discard the unsent drafts of the given outreaches (all unsent demo leads when none are given), return
    those leads to ENRICHED and draft them again. A lead with a sent or received message is refused and left
    exactly as it is. Siblings of one lead (same contact and post, one per channel) are redone as one group."""
    require_sender_name()
    result = ForceResult()
    if outreach_ids:
        wanted = []
        for outreach_id in dict.fromkeys(outreach_ids):
            outreach = repository.get_outreach(db, outreach_id)
            if outreach is None:
                result.refused.append((outreach_id, "no such outreach"))
            else:
                wanted.append(outreach)
    else:
        stmt = (
            select(Outreach)
            .join(Post, Post.id == Outreach.post_id)
            .where(Post.url.contains(DEMO_POST_MARKER))
            .order_by(Outreach.id)
        )
        wanted = list(db.scalars(stmt))

    groups: dict[tuple[int, int | None], list[Outreach]] = {}
    for outreach in wanted:
        key = (outreach.contact_id, outreach.post_id)
        if key not in groups:
            siblings = select(Outreach).where(Outreach.contact_id == key[0], Outreach.post_id == key[1])
            groups[key] = list(db.scalars(siblings.order_by(Outreach.id)))

    keep: list[Outreach] = []
    for group in groups.values():
        why = reset_refusal(db, group)
        if why:
            result.refused.append((group[0].id, why))
            continue
        ids = [o.id for o in group]
        unsent = list(
            db.scalars(
                select(Message).where(
                    Message.outreach_id.in_(ids),
                    Message.direction == MessageDirection.OUTBOUND,
                    Message.status.in_(DISCARDABLE),
                )
            )
        )
        _dismiss_attention(db, {m.id for m in unsent})
        for message in unsent:
            db.delete(message)
        for sibling in group[1:]:  # one outreach per lead and post; drafting recreates the other channel
            db.delete(sibling)
        group[0].state = OutreachState.ENRICHED
        result.discarded += len(unsent)
        keep.append(group[0])
    db.commit()

    drafter = Drafter(db=db)
    for outreach in keep:
        try:
            result.drafted += await drafter.draft_outreach(outreach)
        except Exception as exc:
            logger.error(f"Drafting failed for outreach {outreach.id}: {exc}")
            result.refused.append((outreach.id, f"drafting failed: {type(exc).__name__}: {exc}"))
    return result
