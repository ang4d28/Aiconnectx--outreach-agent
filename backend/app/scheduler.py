"""Scheduler process: runs every job on its schedule (python -m app.scheduler).

APScheduler with the Postgres job store (table apscheduler_jobs, no Redis).
Jobs are module-level functions so the store can reference them by name.

Per-job error handling: the first failure creates a channel_error attention item
and a WhatsApp alert. After FAILURE_THRESHOLD consecutive failures the job's
channel is disabled (settings key <channel>_disabled) with a second alert, so a
broken channel stops being hammered. Re-enable by setting the key to false
(PATCH /api/settings/<key>, or `python -m app.channels.linkedin enable`).

LinkedIn session: linkedin_session checks the persistent profile every 6 hours, and every LinkedIn
work batch (send queue, connection poll, replies) re-checks it first unless a check in the last 30
minutes was fine. needs_login stops all LinkedIn actions and opens a "Reconnect LinkedIn" attention
item (app.channels.status). LINKEDIN_ENABLED=false keeps every LinkedIn job off.

Send-window jobs run only between SEND_WINDOW_START and SEND_WINDOW_END (IST).
DRY_RUN is respected by the workers themselves; the startup log says which mode
is active.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from dataclasses import dataclass
from datetime import time
from typing import Any, Awaitable, Callable

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.channels.linkedin import DISABLED_KEY as LINKEDIN_DISABLED_KEY
from app.channels.linkedin import LinkedInError
from app.channels.outlook import GraphError, OutlookAuthError
from app.channel_config import missing_settings, unregistered_jobs
from app import redact
from app.config import settings, warn_env_overrides
from app.db import SessionLocal
from app.db.models import AttentionItem, AttentionType, Setting
from app.workers.send_queue import EMAIL_DISABLED_KEY, in_send_window, is_truthy, parse_hhmm, utc_now

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3
FAILURES_PREFIX = "job_failures:"
SHEETS_DISABLED_KEY = "sheets_disabled"
DISABLED_KEYS = {
    "linkedin": LINKEDIN_DISABLED_KEY,
    "outlook": EMAIL_DISABLED_KEY,
    "sheets": SHEETS_DISABLED_KEY,
}
POST_MONITOR_MINUTES = 150  # every 2.5 hours
LINKEDIN_SESSION_HOURS = 6
SHORT_GRACE = 300
DAILY_GRACE = 3600


# ─── Failure handling ──────────────────────────────────────


def classify_channel(exc: BaseException, default: str | None) -> str | None:
    if isinstance(exc, LinkedInError):
        return "linkedin"
    if isinstance(exc, (GraphError, OutlookAuthError)):
        return "outlook"
    return default


class JobMonitor:
    """Tracks consecutive failures per job and escalates: attention item, alert, disable channel."""

    def __init__(
        self,
        session_factory: Callable[[], Any] | None = None,
        notify: Callable[[str], Awaitable[Any]] | None = None,
    ):
        self._session_factory = session_factory or SessionLocal
        self._notify = notify

    async def alert(self, text: str) -> None:
        try:
            notify = self._notify
            if notify is None:
                from app.workers.review import whatsapp_notify as notify
            await notify(text)
        except Exception as exc:
            logger.error(f"WhatsApp alert failed: {exc}")

    @staticmethod
    def _get(db: Any, key: str) -> str | None:
        row = db.get(Setting, key)
        return row.value if row else None

    @staticmethod
    def _put(db: Any, key: str, value: str) -> None:
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value

    def apply_overrides(self) -> None:
        """Pick up settings changed on the dashboard (caps, DRY_RUN, hold minutes, TPO ...)."""
        from app.runtime_config import apply_overrides

        db = self._session_factory()
        try:
            apply_overrides(db)
        except Exception as exc:
            logger.error(f"Could not load dashboard settings: {exc}")
        finally:
            db.close()

    def is_channel_disabled(self, channel: str | None) -> bool:
        if channel == "linkedin" and not settings.linkedin_enabled:
            return True
        if channel not in DISABLED_KEYS:
            return False
        db = self._session_factory()
        try:
            return is_truthy(self._get(db, DISABLED_KEYS[channel]))
        finally:
            db.close()

    def record_success(self, job: str) -> None:
        db = self._session_factory()
        try:
            if int(self._get(db, FAILURES_PREFIX + job) or 0):
                self._put(db, FAILURES_PREFIX + job, "0")
                db.commit()
        finally:
            db.close()

    async def record_failure(self, job: str, exc: BaseException, channel: str | None) -> int:
        channel = classify_channel(exc, channel)
        db = self._session_factory()
        try:
            failures = int(self._get(db, FAILURES_PREFIX + job) or 0) + 1
            self._put(db, FAILURES_PREFIX + job, str(failures))
            disable = channel in DISABLED_KEYS and failures == FAILURE_THRESHOLD
            if disable:
                self._put(db, DISABLED_KEYS[channel], "true")
            escalate = failures == 1 or disable
            if escalate:
                db.add(AttentionItem(
                    type=AttentionType.CHANNEL_ERROR,
                    payload={
                        "channel": channel or "scheduler",
                        "job": job,
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                        "failures": failures,
                        "disabled": disable,
                    },
                ))
            db.commit()
        finally:
            db.close()
        logger.error(f"Job {job} failed ({failures} in a row): {exc}", exc_info=exc)
        if disable:
            await self.alert(
                f"Job {job} failed {failures} times in a row ({type(exc).__name__}: {str(exc)[:200]}). "
                f"The {channel} channel is now disabled. Fix it, then set {DISABLED_KEYS[channel]} "
                f"to false (PATCH /api/settings/{DISABLED_KEYS[channel]})."
            )
        elif escalate:
            await self.alert(f"Job {job} failed: {type(exc).__name__}: {str(exc)[:300]}")
        return failures


_monitor: JobMonitor | None = None


def monitor() -> JobMonitor:
    global _monitor
    if _monitor is None:
        _monitor = JobMonitor()
    return _monitor


async def guarded(
    job: str,
    body: Callable[[], Awaitable[Any]],
    channel: str | None = None,
    window_only: bool = False,
    mon: JobMonitor | None = None,
) -> Any:
    """Run one job body with the shared gating and error handling. Never raises."""
    mon = mon or monitor()
    mon.apply_overrides()
    if window_only and not in_send_window(utc_now()):
        logger.info(f"Job {job} skipped: outside the send window")
        return None
    if mon.is_channel_disabled(channel):
        logger.info(f"Job {job} skipped: {channel} channel is disabled")
        return None
    try:
        result = await body()
    except Exception as exc:
        await mon.record_failure(job, exc, channel)
        return None
    mon.record_success(job)
    logger.info(f"Job {job} done: {result}")
    return result


# ─── Job bodies ────────────────────────────────────────────


async def in_session(work: Callable[[Any], Awaitable[Any]]) -> Any:
    db = SessionLocal()
    try:
        return await work(db)
    finally:
        db.close()


async def linkedin_preflight() -> str:
    """Session check before a LinkedIn work batch (skipped when a recent check was fine). Never raises."""
    from app.channels.status import linkedin_preflight as preflight

    return await preflight()


async def run_linkedin_session() -> Any:
    from app.channels.status import check_linkedin

    result = await check_linkedin()
    if result.state == "error":
        raise RuntimeError(f"LinkedIn session check failed: {result.detail}")
    return result.state


async def run_post_monitor() -> Any:
    from app.workers.post_monitor import run_once

    return await run_once()


async def run_company_finder() -> Any:
    from app.workers.pipeline import Pipeline

    return await in_session(lambda db: Pipeline(db=db).run_once())


async def run_send_queue(start_holds: bool) -> Any:
    from app.workers.send_queue import SendQueue

    await linkedin_preflight()
    return await in_session(lambda db: SendQueue(db=db).run_once(start_holds=start_holds))


async def run_connection_poll() -> Any:
    from app.channels.linkedin import get_client
    from app.workers.replies import ReplyWorker

    async def work(db: Any) -> int:
        return await ReplyWorker(db=db).check_connections(get_client())

    await linkedin_preflight()
    return await in_session(work)


async def run_replies() -> Any:
    from app.workers.replies import ReplyWorker

    await linkedin_preflight()
    return await in_session(lambda db: ReplyWorker(db=db).run_once(check_connections=False))


async def run_followups() -> Any:
    from app.workers.followups import FollowUpWorker

    def work() -> Any:
        db = SessionLocal()
        try:
            return FollowUpWorker(db=db).run_once()
        finally:
            db.close()

    return await asyncio.to_thread(work)


async def run_sheets_sync() -> Any:
    from app.channels.sheets import sheets_enabled, sync_and_record

    if not sheets_enabled():
        return "skipped: GOOGLE_SHEET_ID not set"
    report = await in_session(lambda db: sync_and_record(db))
    if report is None:
        raise RuntimeError("Google Sheets sync failed (see logs)")
    return report


async def run_daily_summary() -> Any:
    from app.workers.reports import send_daily_summary_if_due

    return await in_session(lambda db: send_daily_summary_if_due(db))


# ─── Jobs (module-level so the Postgres store can reference them) ──

_send_lock: asyncio.Lock | None = None


def send_lock() -> asyncio.Lock:
    """Sending jobs never overlap, or two runs could pick up the same approved message."""
    global _send_lock
    if _send_lock is None:
        _send_lock = asyncio.Lock()
    return _send_lock


async def job_post_monitor() -> None:
    await guarded("post_monitor", run_post_monitor)


async def job_company_finder() -> None:
    await guarded("company_finder", run_company_finder)


async def job_send_queue() -> None:
    async with send_lock():
        await guarded("send_queue", lambda: run_send_queue(True), window_only=True)


async def job_release_held() -> None:
    if send_lock().locked():
        return  # a full queue run is already releasing messages
    async with send_lock():
        await guarded("release_held", lambda: run_send_queue(False), window_only=True)


async def job_connection_poll() -> None:
    await guarded("connection_poll", run_connection_poll, channel="linkedin", window_only=True)


async def job_replies() -> None:
    await guarded("replies", run_replies, window_only=True)


async def job_linkedin_session() -> None:
    await guarded("linkedin_session", run_linkedin_session, channel="linkedin")


async def job_followups() -> None:
    await guarded("followups", run_followups)


async def job_sheets_sync() -> None:
    await guarded("sheets_sync", run_sheets_sync, channel="sheets")


async def job_daily_summary() -> None:
    await guarded("daily_summary", run_daily_summary)


# ─── Schedule ──────────────────────────────────────────────


@dataclass(frozen=True)
class JobSpec:
    id: str
    func: Callable[[], Awaitable[None]]
    trigger: BaseTrigger
    grace: int = SHORT_GRACE


def window_hours() -> tuple[int, int]:
    """First and last whole hour inside the send window (end is exclusive)."""
    start = parse_hhmm(settings.send_window_start)
    end = parse_hhmm(settings.send_window_end)
    last = end.hour - 1 if end == time(end.hour, 0) else end.hour
    return start.hour, max(start.hour, last)


def build_specs() -> list[JobSpec]:
    tz = settings.send_window_tz
    first, last = window_hours()
    hours = f"{first}-{last}"
    specs = [
        JobSpec("post_monitor", job_post_monitor, IntervalTrigger(minutes=POST_MONITOR_MINUTES, timezone=tz)),
        JobSpec("company_finder", job_company_finder, CronTrigger(hour=8, minute=0, timezone=tz), DAILY_GRACE),
        JobSpec("send_queue", job_send_queue, CronTrigger(hour=hours, minute="*/10", timezone=tz)),
        JobSpec("release_held", job_release_held, CronTrigger(hour=hours, minute="*", timezone=tz), 60),
        JobSpec("connection_poll", job_connection_poll, CronTrigger(hour=f"{first}-{last}/2", minute=0, timezone=tz)),
        JobSpec("replies", job_replies, CronTrigger(hour=hours, minute="0,30", timezone=tz)),
        JobSpec("linkedin_session", job_linkedin_session, IntervalTrigger(hours=LINKEDIN_SESSION_HOURS, timezone=tz)),
        JobSpec("followups", job_followups, CronTrigger(minute=0, timezone=tz)),
        JobSpec("sheets_sync", job_sheets_sync, CronTrigger(minute="15,45", timezone=tz)),
        JobSpec("daily_summary", job_daily_summary, CronTrigger(hour=20, minute=0, timezone=tz), DAILY_GRACE),
    ]
    skipped = unregistered_jobs()  # a channel with missing settings gets none of its own jobs
    return [spec for spec in specs if spec.id not in skipped]


def build_scheduler(jobstore_url: str | None = None) -> AsyncIOScheduler:
    """Jobs live in the SQLAlchemy store at jobstore_url, or in memory when it is None."""
    jobstores = {}
    if jobstore_url:
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

        jobstores["default"] = SQLAlchemyJobStore(url=jobstore_url)
    scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        timezone=settings.send_window_tz,
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    for spec in build_specs():
        scheduler.add_job(
            spec.func,
            spec.trigger,
            id=spec.id,
            replace_existing=True,
            misfire_grace_time=spec.grace,
        )
    return scheduler


def prune_stale_jobs(scheduler: AsyncIOScheduler) -> list[str]:
    """Drop jobs left in the Postgres store by an older version of the schedule."""
    wanted = {spec.id for spec in build_specs()}
    removed: list[str] = []
    for job in scheduler.get_jobs():
        if job.id not in wanted:
            try:
                scheduler.remove_job(job.id)
                removed.append(job.id)
            except JobLookupError:
                pass
    return removed


def describe(scheduler: AsyncIOScheduler) -> list[str]:
    lines = []
    for job in sorted(scheduler.get_jobs(), key=lambda j: j.id):
        lines.append(f"{job.id:16} {job.trigger}")
    return lines


# ─── Entry point ───────────────────────────────────────────


async def serve() -> None:
    scheduler = build_scheduler(settings.database_url)
    scheduler.start()
    removed = prune_stale_jobs(scheduler)
    mode = "DRY_RUN (nothing is sent)" if settings.dry_run else "LIVE (messages will be sent)"
    logger.info(f"Scheduler started in {mode}; send window {settings.send_window_start}-"
                f"{settings.send_window_end} {settings.send_window_tz}")
    if removed:
        logger.info(f"Removed stale jobs: {', '.join(removed)}")
    logger.info(startup_line())
    for job, channel in unregistered_jobs().items():
        logger.warning(f"Job {job} not registered: {channel} settings missing ({', '.join(missing_settings(channel))})")
    for line in describe(scheduler):
        logger.info(f"  {line}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    await stop.wait()
    logger.info("Scheduler stopping")
    scheduler.shutdown(wait=False)


async def check() -> None:
    """Build the schedule without a database and print it (python -m app.scheduler --check)."""
    scheduler = build_scheduler()
    scheduler.start(paused=True)
    mode = "DRY_RUN" if settings.dry_run else "LIVE"
    print(f"Mode: {mode}; window {settings.send_window_start}-{settings.send_window_end} {settings.send_window_tz}")
    for job in sorted(scheduler.get_jobs(), key=lambda j: j.id):
        print(f"{job.id:16} next={job.next_run_time:%Y-%m-%d %H:%M %Z}  {job.trigger}")
    scheduler.shutdown(wait=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    redact.install()
    warn_env_overrides()
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    asyncio.run(check() if "--check" in sys.argv[1:] else serve())


if __name__ == "__main__":
    main()
