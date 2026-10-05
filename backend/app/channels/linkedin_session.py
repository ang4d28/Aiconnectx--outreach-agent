"""LinkedIn session: is the persistent profile signed in, and as whom.

check_session()      opens linkedin.com/feed with the profile in LINKEDIN_PROFILE_DIR. If the feed
                     loads it reads the signed-in account's display name and profile URL; a login,
                     authwall or checkpoint page (or a captcha) means needs_login.
interactive_login()  opens a VISIBLE browser (LINKEDIN_HEADLESS is ignored) at linkedin.com/login and
                     waits up to 5 minutes for the feed while the owner signs in by hand.

Only the page URL, the display name and the profile link are ever read. Passwords and cookie
values are never read, logged or stored; the browser profile keeps the session by itself.
LINKEDIN_ENABLED=false means no browser is opened at all.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urlsplit

from app.channels.linkedin import (
    BASE_URL,
    SELECTORS,
    PageDriver,
    blocked_reason,
    normalize_profile_url,
    playwright_page,
    session_lock,
)
from app.config import settings

logger = logging.getLogger(__name__)

CONNECTED = "connected"
NEEDS_LOGIN = "needs_login"
DISABLED = "disabled"
ERROR = "error"
STATES = (CONNECTED, NEEDS_LOGIN, DISABLED, ERROR)

FEED_URL = f"{BASE_URL}/feed/"
LOGIN_URL = f"{BASE_URL}/login"
LOGIN_TIMEOUT_SECONDS = 300
LOGIN_POLL_SECONDS = 2.0
FEED_WAIT_MS = 20_000
NAME_MAX = 120
LOGIN_COMMAND = "python -m app.cli linkedin-login"

# Unverified against live LinkedIn, like SELECTORS in linkedin.py; each entry lists fallbacks.
SESSION_SELECTORS = {
    "feed": "div.feed-identity-module, div.scaffold-finite-scroll, main.scaffold-layout__main, "
            "img.global-nav__me-photo",
    "name": "div.feed-identity-module__actor-meta a div.t-16, div.feed-identity-module__actor-meta .t-bold, "
            "div.profile-card-member-details h3",
    "me_photo": "img.global-nav__me-photo",
    "profile_link": 'div.feed-identity-module a[href*="/in/"], a.profile-card-profile-link[href*="/in/"], '
                    'div.profile-card a[href*="/in/"]',
}
# Anything that tells us the page has settled: the feed, or one of the pages that need a login.
READY = ", ".join((SESSION_SELECTORS["feed"], SELECTORS["login_wall"], SELECTORS["captcha"]))

Launcher = Callable[..., Any]  # launcher(headless=...) -> async context manager yielding a Playwright page
T = TypeVar("T")


@dataclass(frozen=True)
class SessionStatus:
    state: str
    account_name: str | None = None
    profile_url: str | None = None
    detail: str | None = None


def clean_name(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    for prefix in ("Photo of ", "View profile of "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text[:NAME_MAX] or None


def on_feed(url: str) -> bool:
    return urlsplit(url).path.startswith("/feed")


async def read_session(driver: PageDriver) -> SessionStatus:
    """Classify the page the browser is on right now."""
    url = driver.url
    reason = blocked_reason(url)
    if reason:
        return SessionStatus(NEEDS_LOGIN, detail=reason)
    if await driver.exists(SELECTORS["captcha"]):
        return SessionStatus(NEEDS_LOGIN, detail="captcha")
    if await driver.exists(SELECTORS["login_wall"]):
        return SessionStatus(NEEDS_LOGIN, detail="login_wall")
    if urlsplit(url).path in ("", "/"):
        return SessionStatus(NEEDS_LOGIN, detail="signed_out")  # the public home page
    if not on_feed(url) or not await driver.exists(SESSION_SELECTORS["feed"]):
        return SessionStatus(ERROR, detail="The LinkedIn feed did not load")
    name = clean_name(await driver.text(SESSION_SELECTORS["name"]))
    if name is None:
        name = clean_name(await driver.attr(SESSION_SELECTORS["me_photo"], "alt"))
    href = await driver.attr(SESSION_SELECTORS["profile_link"], "href")
    profile = normalize_profile_url(href if href.startswith("http") else BASE_URL + href) if href else None
    return SessionStatus(CONNECTED, account_name=name, profile_url=profile)


def _driver(page: Any) -> PageDriver:
    return PageDriver(page, random.Random(), asyncio.sleep)


async def check_session(launcher: Launcher | None = None) -> SessionStatus:
    """Open the feed with the persistent profile and report the session state. Never raises."""
    if not settings.linkedin_enabled:
        return SessionStatus(DISABLED, detail="LINKEDIN_ENABLED is false")
    launcher = launcher or playwright_page
    try:
        async with session_lock():
            async with launcher() as page:
                driver = _driver(page)
                await driver.goto(FEED_URL)
                if not blocked_reason(driver.url):
                    await driver.wait_for(READY, FEED_WAIT_MS)
                status = await read_session(driver)
    except Exception as exc:
        logger.error(f"LinkedIn session check failed: {type(exc).__name__}")
        return SessionStatus(ERROR, detail=f"{type(exc).__name__}: {exc}"[:300])
    logger.info(f"LinkedIn session check: {status.state}")
    return status


async def interactive_login(
    launcher: Launcher | None = None,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    poll: float = LOGIN_POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SessionStatus:
    """Visible browser at /login on the persistent profile; returns once the feed shows (or on timeout)."""
    if not settings.linkedin_enabled:
        return SessionStatus(DISABLED, detail="LINKEDIN_ENABLED is false")
    launcher = launcher or playwright_page
    try:
        async with session_lock():
            async with launcher(headless=False) as page:
                driver = _driver(page)
                await driver.goto(LOGIN_URL)  # an existing session redirects straight to the feed
                deadline = clock() + timeout
                while True:
                    if on_feed(driver.url) and await driver.wait_for(SESSION_SELECTORS["feed"], 5_000):
                        status = await read_session(driver)
                        if status.state == CONNECTED:
                            return status
                    if clock() >= deadline:
                        minutes = round(timeout / 60)
                        return SessionStatus(NEEDS_LOGIN, detail=f"Timed out after {minutes} minutes waiting for the feed")
                    await sleep(poll)
    except Exception as exc:
        logger.error(f"LinkedIn login failed: {type(exc).__name__}")
        return SessionStatus(ERROR, detail=f"{type(exc).__name__}: {exc}"[:300])


async def in_own_loop(factory: Callable[[], Awaitable[T]]) -> T:
    """Run browser work on a private event loop in a worker thread (used by the API).

    Uvicorn may run a selector loop on Windows, which cannot start the browser process, and a
    5-minute login must not hold up the API's own loop.
    """

    def run() -> T:
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        try:
            return loop.run_until_complete(factory())
        finally:
            loop.close()

    return await asyncio.to_thread(run)
