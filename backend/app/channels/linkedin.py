"""LinkedIn channel: Playwright automation of the owner's own account.

- Persistent browser profile at LINKEDIN_PROFILE_DIR. The first login is done by
  hand (`python -m app.cli linkedin-login`); no password is ever stored. The
  session check and the login live in app.channels.linkedin_session.
- LINKEDIN_ENABLED=false keeps the channel off: no browser is opened at all.
- One browser session at a time, human pacing: random 5-25s pauses between
  steps, scrolling, per-character typing with variance.
- LINKEDIN_DAILY_CAP is shared by every write action (comment, connection note,
  DM, reply), counted per IST day. Reads (replies, connection checks) are free.
- On a captcha, security checkpoint or login wall the client stops immediately:
  the channel is disabled, the global pause is set, a channel_error attention
  item is created and the owner gets a WhatsApp alert. Nothing tries to get past
  the check; the owner resolves it by hand and re-enables the channel
  (`python -m app.channels.linkedin enable`, then START on WhatsApp).
- DRY_RUN: write actions are logged and skipped; reads still run.

LinkedIn changes its markup often; every selector lives in SELECTORS.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from app.channels.base import IncomingReply, Thread
from app.config import settings
from app.workers.send_queue import IST, PAUSE_KEY, as_utc, utc_now

logger = logging.getLogger(__name__)

BASE_URL = "https://www.linkedin.com"
CONNECTION_NOTE_MAX = 200
CONNECT_PENDING_DAYS = 14
MIN_STEP_DELAY = 5.0
MAX_STEP_DELAY = 25.0
MAX_THREADS_PER_POLL = 15
DISABLED_KEY = "linkedin_disabled"
DISABLED_REASON_KEY = "linkedin_disabled_reason"
ACTIONS_PREFIX = "linkedin_actions:"
BLOCKED_URL_MARKERS = ("/checkpoint/", "/authwall", "/login", "/uas/login", "/signup")

SELECTORS = {
    # Blockers
    "captcha": 'iframe[src*="captcha"], #captcha-internal, form#captcha-challenge, '
               'main:has-text("quick security check")',
    "login_wall": "div.authwall-join-form, form.login__form",
    # Profile
    "first_degree": 'main section span.dist-value:has-text("1st")',
    "pending": 'main section button[aria-label^="Pending"]',
    "connect": 'main section button[aria-label^="Invite"][aria-label$="to connect"]',
    "more": 'main section button[aria-label="More actions"]',
    "more_connect": 'div[role="menu"] [aria-label^="Invite"][aria-label$="to connect"]',
    "add_note": 'div[role="dialog"] button[aria-label="Add a note"]',
    "note_box": 'div[role="dialog"] textarea[name="message"]',
    "send_invite": 'div[role="dialog"] button[aria-label^="Send"]',
    "note_limit": 'div[role="dialog"]:has-text("personalized invitations")',
    "message": 'main section button[aria-label^="Message"]',
    # Messaging
    "msg_box": 'div.msg-form__contenteditable[contenteditable="true"]',
    "msg_send": "button.msg-form__send-button",
    "conversations": "li.msg-conversation-listitem",
    "conv_snippet": ".msg-conversation-card__message-snippet",
    "conv_time": "time.msg-conversation-listitem__time-stamp",
    "conv_link": "a.msg-conversation-listitem__link",
    "thread_profile": "a.msg-thread__link-to-profile",
    "thread_messages": ".msg-s-event-listitem__body",
    # Posts
    "comment_button": 'button[aria-label="Comment"]',
    "comment_box": 'div.ql-editor[contenteditable="true"]',
    "comment_submit": 'button[class*="comments-comment-box__submit-button"]',
}


class LinkedInError(Exception):
    pass


class LinkedInBlocked(LinkedInError):
    """Captcha, checkpoint or login wall. The channel is now disabled and paused."""


class LinkedInUnavailable(LinkedInError):
    """Channel disabled by an earlier block, or today's action cap is used up."""


class LinkedInActionFailed(LinkedInError):
    """The page did not offer the expected control (e.g. no Connect button)."""


# ─── Pure helpers ──────────────────────────────────────────


def normalize_profile_url(url: str) -> str:
    """Canonical profile URL, also used as the DM thread id."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit(("https", "www.linkedin.com", path, "", "")).lower()


def reply_id(profile_url: str, text: str) -> str:
    digest = hashlib.sha1(f"{normalize_profile_url(profile_url)}|{text.strip()}".encode()).hexdigest()
    return f"li-{digest[:16]}"


def blocked_reason(url: str) -> str | None:
    path = urlsplit(url).path
    for marker in BLOCKED_URL_MARKERS:
        if path.startswith(marker):
            return "checkpoint" if "checkpoint" in marker else "login_wall"
    return None


_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_list_time(text: str, now: datetime) -> date | None:
    """IST day of a conversation-list timestamp ("2:15 PM", "Yesterday", "Tue", "Oct 3", "Oct 3, 2025")."""
    today = as_utc(now).astimezone(IST).date()
    value = text.strip().lower()
    if not value:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}\s*(am|pm)?", value) or value in {"now", "just now"}:
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    if value[:3] in _WEEKDAYS and len(value) <= 9:
        back = (today.weekday() - _WEEKDAYS.index(value[:3])) % 7 or 7
        return today - timedelta(days=back)
    try:
        return datetime.strptime(value.title(), "%b %d, %Y").date()
    except ValueError:
        pass
    try:
        parsed = datetime.strptime(f"{value.title()} {today.year}", "%b %d %Y").date()
    except ValueError:
        return None
    return parsed if parsed <= today else parsed.replace(year=today.year - 1)


# ─── Page driver (thin wrapper so tests can replace Playwright) ──


class PageDriver:
    def __init__(self, page: Any, rng: random.Random, sleep: Callable[[float], Awaitable[None]]):
        self.page = page
        self.rng = rng
        self.sleep = sleep

    @property
    def url(self) -> str:
        return self.page.url

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def exists(self, selector: str) -> bool:
        return await self.page.locator(selector).count() > 0

    async def count(self, selector: str) -> int:
        return await self.page.locator(selector).count()

    async def click(self, selector: str) -> None:
        await self.page.locator(selector).first.click()

    async def click_nth(self, selector: str, index: int, child: str) -> None:
        await self.page.locator(selector).nth(index).locator(child).first.click()

    async def text_nth(self, selector: str, index: int, child: str) -> str:
        locator = self.page.locator(selector).nth(index).locator(child)
        return (await locator.first.inner_text()) if await locator.count() else ""

    async def texts(self, selector: str) -> list[str]:
        return await self.page.locator(selector).all_inner_texts()

    async def text(self, selector: str) -> str:
        locator = self.page.locator(selector)
        return (await locator.first.inner_text()) if await locator.count() else ""

    async def wait_for(self, selector: str, timeout_ms: int) -> bool:
        """Wait until the selector is on the page; False on timeout (or any page error)."""
        try:
            await self.page.wait_for_selector(selector, timeout=timeout_ms)
        except Exception:
            return False
        return True

    async def attr(self, selector: str, name: str) -> str | None:
        locator = self.page.locator(selector)
        return (await locator.first.get_attribute(name)) if await locator.count() else None

    async def scroll(self) -> None:
        await self.page.mouse.wheel(0, self.rng.randint(250, 900))

    async def type_text(self, selector: str, text: str) -> None:
        await self.page.locator(selector).first.click()
        for char in text:
            await self.page.keyboard.type(char)
            pause = self.rng.uniform(0.04, 0.18)
            if self.rng.random() < 0.04:
                pause += self.rng.uniform(0.3, 0.9)
            await self.sleep(pause)


Launcher = Callable[[], Any]  # returns an async context manager yielding a Playwright page


@asynccontextmanager
async def playwright_page(headless: bool | None = None) -> AsyncIterator[Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            settings.linkedin_profile_dir,
            headless=settings.linkedin_headless if headless is None else headless,
            viewport={"width": 1366, "height": 860},
            locale="en-US",
        )
        try:
            yield context.pages[0] if context.pages else await context.new_page()
        finally:
            await context.close()


# ─── State: disabled flag, action counter, pause, attention ──


class DbStore:
    """Short-lived DB sessions so the browser session never holds a transaction open."""

    def __init__(self, session_factory: Callable[[], Any] | None = None):
        self._session_factory = session_factory

    def _session(self) -> Any:
        if self._session_factory is None:
            from app.db import SessionLocal

            self._session_factory = SessionLocal
        return self._session_factory()

    def _get(self, key: str) -> str | None:
        from app.db.models import Setting

        db = self._session()
        try:
            row = db.get(Setting, key)
            return row.value if row else None
        finally:
            db.close()

    def _put(self, values: dict[str, str]) -> None:
        from app.db.models import Setting

        db = self._session()
        try:
            for key, value in values.items():
                row = db.get(Setting, key)
                if row is None:
                    db.add(Setting(key=key, value=value))
                else:
                    row.value = value
            db.commit()
        finally:
            db.close()

    def is_disabled(self) -> bool:
        from app.workers.send_queue import is_truthy

        return is_truthy(self._get(DISABLED_KEY))

    def set_enabled(self) -> None:
        self._put({DISABLED_KEY: "false", DISABLED_REASON_KEY: ""})

    def actions_on(self, day: date) -> int:
        return int(self._get(f"{ACTIONS_PREFIX}{day.isoformat()}") or 0)

    def increment(self, day: date) -> None:
        self._put({f"{ACTIONS_PREFIX}{day.isoformat()}": str(self.actions_on(day) + 1)})

    def disable_and_pause(self, reason: str, url: str) -> None:
        from app.db.models import AttentionItem, AttentionType

        self._put({DISABLED_KEY: "true", DISABLED_REASON_KEY: reason, PAUSE_KEY: "true"})
        db = self._session()
        try:
            db.add(AttentionItem(
                type=AttentionType.CHANNEL_ERROR,
                payload={"channel": "linkedin", "reason": reason, "url": url},
            ))
            db.commit()
        finally:
            db.close()


# ─── Client ────────────────────────────────────────────────


_session_locks: dict[Any, asyncio.Lock] = {}


def session_lock() -> asyncio.Lock:
    """One browser session per process and event loop (Chromium also locks the profile dir)."""
    loop = asyncio.get_running_loop()
    if loop not in _session_locks:
        _session_locks.clear()
        _session_locks[loop] = asyncio.Lock()
    return _session_locks[loop]


class LinkedInClient:
    def __init__(
        self,
        store: Any = None,
        launcher: Launcher | None = None,
        notify: Callable[[str], Awaitable[Any]] | None = None,
        dry_run: bool | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = utc_now,
        daily_cap: int | None = None,
    ):
        self.store = store or DbStore()
        self.launcher = launcher or playwright_page
        self._notify = notify
        self._dry_run = dry_run
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.clock = clock
        self._daily_cap = daily_cap

    # DRY_RUN and the cap are read live so dashboard changes apply without a restart.
    @property
    def dry_run(self) -> bool:
        return settings.dry_run if self._dry_run is None else self._dry_run

    @property
    def daily_cap(self) -> int:
        return settings.linkedin_daily_cap if self._daily_cap is None else self._daily_cap

    # ─── Availability and cap ──────────────────────────────

    @property
    def available(self) -> bool:
        return settings.linkedin_enabled and not self.store.is_disabled()

    def _today(self) -> date:
        return as_utc(self.clock()).astimezone(IST).date()

    def remaining_today(self) -> int:
        return max(0, self.daily_cap - self.store.actions_on(self._today()))

    def _ensure_available(self) -> None:
        if not settings.linkedin_enabled:
            raise LinkedInUnavailable("LinkedIn is switched off (LINKEDIN_ENABLED=false)")
        if self.store.is_disabled():
            raise LinkedInUnavailable("LinkedIn channel is disabled after a security check")

    # ─── Session and pacing ────────────────────────────────

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[PageDriver]:
        async with session_lock():
            async with self.launcher() as page:
                yield PageDriver(page, self.rng, self.sleep)

    async def _pause(self) -> None:
        await self.sleep(self.rng.uniform(MIN_STEP_DELAY, MAX_STEP_DELAY))

    async def _open(self, driver: PageDriver, url: str) -> None:
        await driver.goto(url)
        await self._check(driver)
        await self._pause()
        await driver.scroll()
        await self._check(driver)

    async def _check(self, driver: PageDriver) -> None:
        reason = blocked_reason(driver.url)
        if reason is None and await driver.exists(SELECTORS["captcha"]):
            reason = "captcha"
        if reason is None and await driver.exists(SELECTORS["login_wall"]):
            reason = "login_wall"
        if reason:
            await self._block(reason, driver.url)

    async def _block(self, reason: str, url: str) -> None:
        logger.error(f"LinkedIn {reason} at {url}: channel disabled, global pause set")
        self.store.disable_and_pause(reason, url)
        text = (
            f"LinkedIn stopped: {reason.replace('_', ' ')} at {url}.\n"
            "The LinkedIn channel is disabled and all sending is paused. Resolve it in the "
            "browser (python -m app.cli linkedin-login), then run "
            "`python -m app.channels.linkedin enable` and reply START."
        )
        try:
            notify = self._notify
            if notify is None:
                from app.workers.review import whatsapp_notify as notify
            await notify(text)
        except Exception as exc:
            logger.error(f"WhatsApp alert for LinkedIn block failed: {exc}")
        raise LinkedInBlocked(reason)

    async def _write(self, label: str, action: Callable[[PageDriver], Awaitable[Any]]) -> Any:
        """Gate every write: DRY_RUN, disabled channel, shared daily cap, single session."""
        if self.dry_run:
            logger.info(f"DRY_RUN: LinkedIn {label} skipped")
            return None
        self._ensure_available()
        if self.remaining_today() <= 0:
            raise LinkedInUnavailable(f"LinkedIn daily cap of {self.daily_cap} actions reached")
        async with self._session() as driver:
            return await action(driver)

    def _record_action(self) -> None:
        """Count a completed write toward LINKEDIN_DAILY_CAP (right after the final click)."""
        self.store.increment(self._today())

    async def _read(self, action: Callable[[PageDriver], Awaitable[Any]]) -> Any:
        self._ensure_available()
        async with self._session() as driver:
            return await action(driver)

    # ─── Actions ───────────────────────────────────────────

    async def comment_on_post(self, post_url: str, text: str) -> None:
        async def run(d: PageDriver) -> None:
            await self._open(d, post_url)
            await d.click(SELECTORS["comment_button"])
            await self._pause()
            await d.type_text(SELECTORS["comment_box"], text)
            await self._pause()
            await d.click(SELECTORS["comment_submit"])
            self._record_action()
            await self._check(d)

        await self._write("comment", run)

    async def send_connection_note(self, profile_url: str, note: str) -> str | None:
        if len(note) > CONNECTION_NOTE_MAX:
            raise ValueError(f"Connection note exceeds {CONNECTION_NOTE_MAX} characters")

        async def run(d: PageDriver) -> None:
            await self._open(d, profile_url)
            if await d.exists(SELECTORS["first_degree"]) or await d.exists(SELECTORS["pending"]):
                logger.info(f"Already connected or pending: {profile_url}")
                return None
            if await d.exists(SELECTORS["connect"]):
                await d.click(SELECTORS["connect"])
            elif await d.exists(SELECTORS["more"]):
                await d.click(SELECTORS["more"])
                await self._pause()
                if not await d.exists(SELECTORS["more_connect"]):
                    raise LinkedInActionFailed("No Connect option on profile")
                await d.click(SELECTORS["more_connect"])
            else:
                raise LinkedInActionFailed("No Connect option on profile")
            await self._pause()
            await self._check(d)
            if await d.exists(SELECTORS["note_limit"]) or not await d.exists(SELECTORS["add_note"]):
                raise LinkedInActionFailed("LinkedIn did not offer a personalised note (monthly note limit?)")
            await d.click(SELECTORS["add_note"])
            await d.type_text(SELECTORS["note_box"], note)
            await self._pause()
            await d.click(SELECTORS["send_invite"])
            self._record_action()
            await self._check(d)
            return None

        return await self._write("connection note", run)

    async def is_connected(self, profile_url: str) -> bool:
        async def run(d: PageDriver) -> bool:
            await self._open(d, profile_url)
            return await d.exists(SELECTORS["first_degree"])

        return await self._read(run)

    async def send_dm(self, profile_url: str, text: str) -> str | None:
        """Returns the normalised profile URL, used as the thread id for reply matching."""

        async def run(d: PageDriver) -> str:
            await self._open(d, profile_url)
            if not await d.exists(SELECTORS["message"]):
                raise LinkedInActionFailed("No Message button (not connected?)")
            await d.click(SELECTORS["message"])
            await self._pause()
            await self._check(d)
            await d.type_text(SELECTORS["msg_box"], text)
            await self._pause()
            await d.click(SELECTORS["msg_send"])
            self._record_action()
            await self._check(d)
            return normalize_profile_url(profile_url)

        return await self._write("DM", run)

    async def read_new_replies(self, since: datetime) -> list[IncomingReply]:
        """Latest message of each recent conversation whose last word is theirs."""
        since_day = as_utc(since).astimezone(IST).date()
        now = self.clock()

        async def run(d: PageDriver) -> list[IncomingReply]:
            await self._open(d, f"{BASE_URL}/messaging/")
            replies: list[IncomingReply] = []
            total = min(await d.count(SELECTORS["conversations"]), MAX_THREADS_PER_POLL)
            for index in range(total):
                day = parse_list_time(await d.text_nth(SELECTORS["conversations"], index, SELECTORS["conv_time"]), now)
                if day is not None and day < since_day:
                    break  # the list is newest first
                snippet = await d.text_nth(SELECTORS["conversations"], index, SELECTORS["conv_snippet"])
                if snippet.strip().lower().startswith("you:"):
                    continue
                await d.click_nth(SELECTORS["conversations"], index, SELECTORS["conv_link"])
                await self._pause()
                await self._check(d)
                href = await d.attr(SELECTORS["thread_profile"], "href")
                bodies = [b.strip() for b in await d.texts(SELECTORS["thread_messages"]) if b.strip()]
                if not href or not bodies:
                    continue
                profile = normalize_profile_url(href if href.startswith("http") else BASE_URL + href)
                replies.append(IncomingReply(
                    message_id=reply_id(profile, bodies[-1]),
                    thread_id=profile,
                    sender=profile,
                    text=bodies[-1],
                    received_at=None,
                ))
            return replies

        return await self._read(run)

    # ─── Channel protocol ──────────────────────────────────

    async def send_message(self, thread: Thread, text: str) -> str | None:
        return await self.send_dm(thread.recipient, text)

    async def reply(self, thread: Thread, text: str) -> str | None:
        return await self.send_dm(thread.recipient, text)


_client: LinkedInClient | None = None


def get_client() -> LinkedInClient:
    global _client
    if _client is None:
        _client = LinkedInClient()
    return _client


# ─── Manual login / re-enable ──────────────────────────────


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "login":
        from app.cli import main as cli_main

        raise SystemExit(cli_main(["linkedin-login"]))
    elif command == "enable":
        DbStore().set_enabled()
        print("LinkedIn channel re-enabled. Reply START on WhatsApp to lift the global pause.")
    else:
        print("Usage: python -m app.channels.linkedin [login|enable]")
