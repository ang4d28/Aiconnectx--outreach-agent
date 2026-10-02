from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal, repository
from app.db.models import (
    ChannelType,
    Contact,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    Post,
)
from app.db.schemas import MessageCreate, OutreachCreate, OutreachUpdate
from app.llm.client import llm_client
from app.llm.prompts import draft_message_prompt

logger = logging.getLogger(__name__)


class DraftedMessage(BaseModel):
    message: str


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
    }


async def draft_message(
    channel: ChannelType, target_info: dict[str, str], context: dict[str, str]
) -> str:
    prompt = draft_message_prompt(channel.value, target_info, context)
    result = await llm_client.complete_json(prompt, DraftedMessage)
    return result.message.strip()


class Drafter:
    def __init__(self, db: Session | None = None):
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
        content = await draft_message(channel, target, context)
        repository.create_message(
            self.db,
            MessageCreate(
                outreach_id=outreach.id,
                direction=MessageDirection.OUTBOUND,
                content=content,
                status=MessageStatus.DRAFTED,
            ),
        )
        repository.update_outreach(
            self.db, outreach, OutreachUpdate(state=OutreachState.DRAFTED)
        )

    async def run_once(self) -> int:
        drafted = 0
        for outreach in self._enriched_outreaches():
            try:
                drafted += await self.draft_outreach(outreach)
            except Exception as exc:
                logger.error(f"Drafting failed for outreach {outreach.id}: {exc}")
        return drafted
