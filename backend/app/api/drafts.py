from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.inquiries import RateLimiter
from app.auth.deps import get_current_user
from app.config import settings
from app.db import SessionLocal, get_session, repository
from app.db.models import ChannelType, DraftVersion, Message, MessageDirection, MessageStatus, Outreach, User
from app.db.schemas import MessageUpdate
from app.llm.client import LLMError
from app.workers import drafter
from app.workers.drafter import DraftRejected, DrafterConfigError, RegenerateRefused

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drafts", tags=["drafts"])

REGENERATIONS_PER_MINUTE = 10
MAX_INSTRUCTION = 200
MAX_JOBS = 20
regenerate_limiter = RateLimiter(limit=REGENERATIONS_PER_MINUTE, window=60)


class DraftRead(BaseModel):
    id: int
    outreach_id: int
    channel: ChannelType
    content: str
    status: MessageStatus
    dry_run: bool


class DraftEdit(BaseModel):
    content: str = Field(..., min_length=1)


def to_read(message: Message, channel: ChannelType) -> DraftRead:
    return DraftRead(
        id=message.id,
        outreach_id=message.outreach_id,
        channel=channel,
        content=message.content,
        status=message.status,
        dry_run=settings.dry_run,
    )


def get_draft_or_404(db: Session, draft_id: int) -> Message:
    message = repository.get_message(db, draft_id)
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    return message


def require_pending(message: Message) -> None:
    if message.status != MessageStatus.DRAFTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Draft is already {message.status.value}"
        )


def channel_of(db: Session, message: Message) -> ChannelType:
    return repository.get_outreach(db, message.outreach_id).channel


def set_status(db: Session, message: Message, new_status: MessageStatus, user: User) -> DraftRead:
    require_pending(message)
    updated = repository.update_message(db, message, MessageUpdate(status=new_status))
    return record_reviewer(db, updated, user)


def record_reviewer(db: Session, message: Message, user: User) -> DraftRead:
    message.reviewed_by = user.id
    db.commit()
    return to_read(message, channel_of(db, message))


@router.get("", response_model=list[DraftRead])
def list_drafts(
    draft_status: MessageStatus = Query(MessageStatus.DRAFTED, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_session),
) -> list[DraftRead]:
    stmt = (
        select(Message, Outreach.channel)
        .join(Outreach, Outreach.id == Message.outreach_id)
        .where(Message.status == draft_status)
        .order_by(Message.id)
        .offset(skip)
        .limit(limit)
    )
    return [to_read(message, channel) for message, channel in db.execute(stmt)]


@router.post("/{draft_id}/approve", response_model=DraftRead)
def approve_draft(
    draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> DraftRead:
    return set_status(db, get_draft_or_404(db, draft_id), MessageStatus.APPROVED, user)


@router.post("/{draft_id}/reject", response_model=DraftRead)
def reject_draft(
    draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> DraftRead:
    return set_status(db, get_draft_or_404(db, draft_id), MessageStatus.REJECTED, user)


@router.patch("/{draft_id}", response_model=DraftRead)
def edit_draft(
    draft_id: int,
    data: DraftEdit,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> DraftRead:
    message = get_draft_or_404(db, draft_id)
    require_pending(message)
    updated = repository.update_message(db, message, MessageUpdate(content=data.content))
    return record_reviewer(db, updated, user)


# ─── Regenerate and restore ────────────────────────────────


class RegenerateRequest(BaseModel):
    instruction: str | None = Field(None, max_length=MAX_INSTRUCTION)


class RestoreRequest(BaseModel):
    version_id: int | None = None  # default: the latest previous version


def draft_state(db: Session, message: Message) -> dict[str, Any]:
    versions = db.scalar(select(func.count()).select_from(DraftVersion).where(DraftVersion.draft_id == message.id))
    return {
        "id": message.id,
        "text": message.edited_text or message.content,
        "subject": message.subject,
        "status": message.status.value,
        "review_status": message.review_status.value if message.review_status else None,
        "hold_until": message.hold_until.isoformat() if message.hold_until else None,
        "regenerated_at": message.regenerated_at.isoformat() if message.regenerated_at else None,
        "has_previous": bool(versions),
    }


def throttle(user: User) -> None:
    if not regenerate_limiter.allow(str(user.id)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"At most {REGENERATIONS_PER_MINUTE} regenerations per minute. Wait a moment and try again.",
        )


def clean_instruction(value: str | None) -> str:
    return " ".join((value or "").split())


def regenerate_error(exc: Exception) -> HTTPException:
    """The HTTP answer for a regeneration that did not happen. The draft itself is untouched."""
    if isinstance(exc, LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, RegenerateRefused):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, DrafterConfigError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if isinstance(exc, DraftRejected):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"The new draft failed the quality checks twice ({exc}). The current text was kept.",
        )
    if isinstance(exc, LLMError):
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"All AI providers failed, so nothing was changed. Try again in a minute. ({exc})",
        )
    raise exc


@router.post("/{draft_id}/regenerate")
async def regenerate_draft(
    draft_id: int,
    data: RegenerateRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    throttle(user)
    instruction = clean_instruction(data.instruction if data else None)
    try:
        message = await drafter.regenerate_message(db, draft_id, instruction, user.id)
    except (LookupError, RegenerateRefused, DrafterConfigError, DraftRejected, LLMError) as exc:
        db.rollback()
        raise regenerate_error(exc) from exc
    return draft_state(db, message)


@router.get("/{draft_id}/versions")
def list_versions(draft_id: int, db: Session = Depends(get_session)) -> list[dict[str, Any]]:
    get_draft_or_404(db, draft_id)
    stmt = select(DraftVersion).where(DraftVersion.draft_id == draft_id).order_by(DraftVersion.id.desc())
    return [
        {
            "id": v.id,
            "text": v.text,
            "subject": v.subject,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "created_by": v.created_by,
        }
        for v in db.scalars(stmt)
    ]


@router.post("/{draft_id}/restore")
async def restore_draft(
    draft_id: int,
    data: RestoreRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        message = await drafter.restore_version(db, draft_id, data.version_id if data else None, user.id)
    except (LookupError, RegenerateRefused) as exc:
        db.rollback()
        raise regenerate_error(exc) from exc
    return draft_state(db, message)


@dataclass
class RegenerateJob:
    id: str
    total: int
    started_by: int
    done: int = 0
    failed: int = 0
    status: str = "running"  # running | finished
    errors: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "errors": self.errors[:20],
        }


JOBS: dict[str, RegenerateJob] = {}
_tasks: set[asyncio.Task[None]] = set()


async def run_regenerate_all(job: RegenerateJob, ids: list[int]) -> None:
    """One draft at a time, each with its own session, so a failure never blocks the rest."""
    for draft_id in ids:
        db = SessionLocal()
        try:
            await drafter.regenerate_message(db, draft_id, "", job.started_by)
            job.done += 1
        except Exception as exc:  # one draft failing (or being sent meanwhile) must not stop the others
            db.rollback()
            job.failed += 1
            try:
                reason = regenerate_error(exc).detail
            except Exception:
                reason = f"{type(exc).__name__}: {exc}"
                logger.exception(f"Regenerating draft {draft_id} failed")
            job.errors.append({"id": draft_id, "reason": reason})
        finally:
            db.close()
    job.status = "finished"


@router.post("/regenerate-all", status_code=status.HTTP_202_ACCEPTED)
async def regenerate_all(
    user: User = Depends(get_current_user), db: Session = Depends(get_session)
) -> dict[str, Any]:
    throttle(user)
    running = next((j for j in JOBS.values() if j.status == "running"), None)
    if running is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A regenerate-all run is already in progress ({running.done}/{running.total})"
        )
    try:
        drafter.require_sender_name()
    except DrafterConfigError as exc:
        raise regenerate_error(exc) from exc
    from app.workers.send_queue import REPLY_STATES  # replies in a conversation are not regenerated

    ids = [
        message.id
        for message, outreach in db.execute(
            select(Message, Outreach)
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(Message.direction == MessageDirection.OUTBOUND, Message.status == MessageStatus.DRAFTED)
            .order_by(Message.id)
        )
        if outreach.state not in REPLY_STATES
    ]
    job = RegenerateJob(id=uuid.uuid4().hex, total=len(ids), started_by=user.id)
    if not ids:
        job.status = "finished"
    JOBS[job.id] = job
    while len(JOBS) > MAX_JOBS:
        JOBS.pop(next(iter(JOBS)))
    if ids:
        task = asyncio.create_task(run_regenerate_all(job, ids))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return job.public()


@router.get("/regenerate-jobs/{job_id}")
def regenerate_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown job")
    return job.public()
