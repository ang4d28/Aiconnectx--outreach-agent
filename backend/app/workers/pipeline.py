"""Company finder: turns newly found hiring posts into drafted outreach.

post_monitor saves relevant posts (with their company). For each NEW post this
picks the right contact (targeting + enrichment), creates an ENRICHED outreach
on the best channel and lets the drafter write the first message. Drafts then
wait for the owner's approval on the dashboard before the send queue touches them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal, repository
from app.db.models import ChannelType, Outreach, OutreachState, Post, PostStatus
from app.db.schemas import OutreachCreate, PostUpdate

logger = logging.getLogger(__name__)

MAX_POSTS_PER_RUN = 25


@dataclass
class PipelineReport:
    targeted: int = 0
    skipped: int = 0
    failed: int = 0
    drafted: int = 0


class Pipeline:
    def __init__(self, db: Any = None, targeting: Any = None, drafter: Any = None):
        self.db = db or SessionLocal()
        self._targeting = targeting
        self._drafter = drafter

    @property
    def targeting(self) -> Any:
        if self._targeting is None:
            from app.workers.targeting import TargetingWorker

            self._targeting = TargetingWorker(db=self.db)
        return self._targeting

    @property
    def drafter(self) -> Any:
        if self._drafter is None:
            from app.workers.drafter import Drafter

            self._drafter = Drafter(db=self.db)
        return self._drafter

    def _new_posts(self) -> list[Post]:
        stmt = (
            select(Post)
            .where(Post.status == PostStatus.NEW)
            .order_by(Post.relevance_score.desc(), Post.id)
            .limit(MAX_POSTS_PER_RUN)
        )
        return list(self.db.scalars(stmt))

    def _set_status(self, post: Post, status: PostStatus) -> None:
        repository.update_post(self.db, post, PostUpdate(status=status))

    async def process_post(self, post: Post) -> str:
        if post.company_id is None:
            self._set_status(post, PostStatus.SKIPPED)
            return "skipped"
        contact = await self.targeting.pick_target(post)
        if contact is None or contact.is_unsubscribed:
            self._set_status(post, PostStatus.SKIPPED)
            return "skipped"
        channel = ChannelType.LINKEDIN if contact.linkedin_url else ChannelType.EMAIL
        if not contact.linkedin_url and not contact.email:
            self._set_status(post, PostStatus.SKIPPED)
            return "skipped"
        if repository.get_outreaches_by_contact(self.db, contact.id, limit=1):
            self._set_status(post, PostStatus.PROCESSED)  # already in the pipeline: one touch per person
            return "skipped"
        repository.create_outreach(
            self.db,
            OutreachCreate(
                contact_id=contact.id, post_id=post.id, channel=channel, state=OutreachState.ENRICHED
            ),
        )
        self._set_status(post, PostStatus.PROCESSED)
        return "targeted"

    async def run_once(self) -> PipelineReport:
        report = PipelineReport()
        for post in self._new_posts():
            try:
                outcome = await self.process_post(post)
            except Exception as exc:
                # Left NEW so tomorrow's run retries it.
                logger.error(f"Targeting failed for post {post.id}: {exc}")
                self.db.rollback()
                report.failed += 1
                continue
            setattr(report, outcome, getattr(report, outcome) + 1)
        report.drafted = await self.drafter.run_once()
        return report
