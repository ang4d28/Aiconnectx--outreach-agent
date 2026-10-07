"""Channel connections (admin only: /api/channels is in ADMIN_ONLY_PREFIXES, app.auth.deps).

GET  /api/channels/status            LinkedIn, Outlook and WhatsApp: state, account name, last checked.
                                     Reads the saved status from the database only. It never opens a
                                     browser or calls an external service, so it is always fast.
POST /api/channels/{channel}/check   start that channel's check in the background and return 202 at once;
                                     the result lands in the saved status (poll GET /status, `checking`
                                     lists the checks still running).
POST /api/channels/linkedin/connect  open a visible LinkedIn login on the machine the backend runs on.
                                     Without a display it answers 409 with the CLI command to run instead.
Only these two POST actions and the scheduler run real checks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.channels import linkedin_session
from app.channels import status as channel_status
from app.channels.linkedin_session import LOGIN_COMMAND, LOGIN_TIMEOUT_SECONDS, in_own_loop
from app.config import settings
from app.db import SessionLocal, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])

_login_task: asyncio.Task | None = None
_check_tasks: dict[str, asyncio.Task] = {}


def has_display() -> bool:
    """Can this process open a visible browser window?"""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def login_running() -> bool:
    return _login_task is not None and not _login_task.done()


def checks_running() -> list[str]:
    return [channel for channel, task in _check_tasks.items() if not task.done()]


@router.get("/status")
def get_channel_status(db: Session = Depends(get_session)) -> dict[str, Any]:
    return {
        "channels": channel_status.all_statuses(db),
        "checking": checks_running(),
        "linkedin_login_running": login_running(),
        "display_available": has_display(),
        "login_command": LOGIN_COMMAND,
    }


async def _run_check(channel: str) -> None:
    """Background body of Check now. Stores the result; never raises."""
    try:
        if channel == "linkedin":
            result = await in_own_loop(linkedin_session.check_session)
            await channel_status.record_linkedin(result)
        else:
            db = SessionLocal()
            try:
                await channel_status.check_other(channel, db)
            finally:
                db.close()
    except Exception as exc:
        logger.error(f"{channel} check failed: {type(exc).__name__}: {exc}")


@router.post("/{channel}/check", status_code=status.HTTP_202_ACCEPTED)
async def check_now(channel: str) -> dict[str, Any]:
    if channel not in channel_status.CHANNELS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown channel")
    if channel == "linkedin" and login_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "The LinkedIn login window is open. Finish signing in first.")
    if channel not in checks_running():  # a second click while one is running joins it
        _check_tasks[channel] = asyncio.create_task(_run_check(channel))
    return {"channel": channel, "checking": True}


async def _run_login() -> None:
    try:
        result = await in_own_loop(linkedin_session.interactive_login)
        await channel_status.record_linkedin(result)
        logger.info(f"LinkedIn login from the dashboard finished: {result.state}")
    except Exception as exc:
        logger.error(f"LinkedIn login from the dashboard failed: {type(exc).__name__}: {exc}")


@router.post("/linkedin/connect", status_code=status.HTTP_202_ACCEPTED)
async def connect_linkedin() -> dict[str, Any]:
    global _login_task
    if not settings.linkedin_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "LinkedIn is switched off (LINKEDIN_ENABLED=false).")
    if not has_display():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The backend runs without a display, so it cannot open a browser window. On the server, run "
            f"`{LOGIN_COMMAND}` from a session with a screen (for example ssh -X, or WSLg), sign in, "
            "then press Check now.",
        )
    if login_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "A LinkedIn login window is already open.")
    _login_task = asyncio.create_task(_run_login())
    minutes = LOGIN_TIMEOUT_SECONDS // 60
    return {
        "started": True,
        "message": f"A browser window opened on the server. Sign in to LinkedIn there within {minutes} minutes; "
        "it closes by itself when the feed loads.",
    }
