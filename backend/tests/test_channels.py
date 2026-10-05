"""LinkedIn session check and login, channel status, /api/channels, CLI and scheduler wiring.

Playwright is always faked (FakePage from test_linkedin); conftest fails any real browser launch.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import cli, scheduler
from app.api import channels as channels_api
from app.api.main import api_router
from app.auth import oauth, security
from app.channels import linkedin_session
from app.channels import status as channel_status
from app.channels.linkedin import DISABLED_KEY, DISABLED_REASON_KEY, SELECTORS, LinkedInUnavailable
from app.channels.linkedin_session import (
    CONNECTED,
    DISABLED,
    ERROR,
    FEED_URL,
    LOGIN_URL,
    NEEDS_LOGIN,
    SESSION_SELECTORS,
    SessionStatus,
    check_session,
    interactive_login,
)
from app.config import settings
from app.db import get_session
from app.db.models import AttentionItem, AttentionStatus, AttentionType, AuthProvider, Setting, User, UserRole
from tests.test_linkedin import FakePage, FakeStore, make_client

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
FEED_DOM = {
    SESSION_SELECTORS["feed"]: 1,
    SESSION_SELECTORS["name"]: "  Jane   Doe ",
}
PROFILE_HREF = "/in/Jane-Doe/?miniProfileUrn=abc"
CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}


class SessionPage(FakePage):
    """FakePage plus wait_for_selector, which the session check uses to let the page settle."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waits: list = []

    async def wait_for_selector(self, selector, timeout=None):
        self.waits.append((selector, timeout))
        if not self.dom.get(selector) and selector != linkedin_session.READY:
            raise TimeoutError(selector)


def launcher_for(page):
    calls: list = []

    @asynccontextmanager
    async def launcher(**kwargs):
        calls.append(kwargs)
        yield page

    launcher.calls = calls
    return launcher


def feed_page(dom=None, href=PROFILE_HREF):
    dom = dict(FEED_DOM if dom is None else dom)
    if href:
        dom[SESSION_SELECTORS["profile_link"]] = 1
    page = SessionPage(pages={FEED_URL: dom})
    if href:
        page.attrs[(SESSION_SELECTORS["profile_link"], "href")] = href
    return page


@pytest.fixture
def linkedin_on(monkeypatch):
    monkeypatch.setattr(settings, "linkedin_enabled", True)


@pytest.fixture
def linkedin_off(monkeypatch):
    monkeypatch.setattr(settings, "linkedin_enabled", False)


def setting(db, key):
    db.expire_all()
    row = db.get(Setting, key)
    return row.value if row else None


def reconnect_items(db):
    db.expire_all()
    return [
        item for item in db.query(AttentionItem).all()
        if (item.payload or {}).get("reason") == channel_status.NEEDS_LOGIN_REASON
    ]


# ─── Session check ─────────────────────────────────────────


class TestCheckSession:
    @pytest.mark.asyncio
    async def test_feed_loads_reads_name_and_profile(self, linkedin_on):
        page = feed_page()
        launcher = launcher_for(page)
        result = await check_session(launcher)
        assert result == SessionStatus(CONNECTED, "Jane Doe", "https://www.linkedin.com/in/jane-doe")
        assert page.visits == [FEED_URL]
        assert launcher.calls == [{}]  # headless follows LINKEDIN_HEADLESS

    @pytest.mark.asyncio
    async def test_name_falls_back_to_the_nav_photo(self, linkedin_on):
        page = feed_page({SESSION_SELECTORS["feed"]: 1, SESSION_SELECTORS["me_photo"]: 1}, href="https://www.linkedin.com/in/jd/")
        page.attrs[(SESSION_SELECTORS["me_photo"], "alt")] = "Photo of Jane Doe"
        result = await check_session(launcher_for(page))
        assert (result.state, result.account_name, result.profile_url) == (
            CONNECTED, "Jane Doe", "https://www.linkedin.com/in/jd"
        )

    @pytest.mark.parametrize(
        ("landing", "dom", "detail"),
        [
            ("https://www.linkedin.com/login?session_redirect=feed", {}, "login_wall"),
            ("https://www.linkedin.com/authwall?trk=1", {}, "login_wall"),
            ("https://www.linkedin.com/checkpoint/challenge/x", {}, "checkpoint"),
            ("https://www.linkedin.com/", {}, "signed_out"),
            (FEED_URL, {SELECTORS["captcha"]: 1}, "captcha"),
            (FEED_URL, {SELECTORS["login_wall"]: 1}, "login_wall"),
        ],
    )
    @pytest.mark.asyncio
    async def test_login_or_checkpoint_means_needs_login(self, linkedin_on, landing, dom, detail):
        page = SessionPage(pages={FEED_URL: dom}, redirects={FEED_URL: landing})
        result = await check_session(launcher_for(page))
        assert result == SessionStatus(NEEDS_LOGIN, detail=detail)

    @pytest.mark.asyncio
    async def test_feed_that_never_renders_is_an_error(self, linkedin_on):
        result = await check_session(launcher_for(SessionPage()))
        assert result.state == ERROR and "feed did not load" in result.detail

    @pytest.mark.asyncio
    async def test_browser_failure_is_an_error_not_an_exception(self, linkedin_on):
        @asynccontextmanager
        async def broken(**kwargs):
            raise RuntimeError("profile is locked")
            yield  # pragma: no cover

        result = await check_session(broken)
        assert result.state == ERROR and "profile is locked" in result.detail

    @pytest.mark.asyncio
    async def test_disabled_never_opens_a_browser(self, linkedin_off):
        launcher = launcher_for(feed_page())
        assert (await check_session(launcher)).state == DISABLED
        assert launcher.calls == []

    @pytest.mark.asyncio
    async def test_reads_no_cookies_or_storage(self, linkedin_on):
        page = feed_page()
        page.context = SimpleNamespace(cookies=AsyncMock(), storage_state=AsyncMock())
        await check_session(launcher_for(page))
        page.context.cookies.assert_not_awaited()
        page.context.storage_state.assert_not_awaited()


class TestInteractiveLogin:
    @pytest.mark.asyncio
    async def test_visible_browser_waits_for_the_feed(self, linkedin_on, monkeypatch):
        monkeypatch.setattr(settings, "linkedin_headless", True)  # ignored for the login
        page = SessionPage()
        launcher = launcher_for(page)
        polls = []

        async def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 3:  # the owner finishes signing in
                page.url = FEED_URL
                page.dom = {**FEED_DOM, SESSION_SELECTORS["profile_link"]: 1}
                page.attrs[(SESSION_SELECTORS["profile_link"], "href")] = PROFILE_HREF

        result = await interactive_login(launcher, timeout=300, poll=2, clock=lambda: 0.0, sleep=sleep)
        assert launcher.calls == [{"headless": False}]
        assert page.visits == [LOGIN_URL]
        assert result.state == CONNECTED and result.account_name == "Jane Doe"
        assert polls == [2, 2, 2]

    @pytest.mark.asyncio
    async def test_existing_session_returns_at_once(self, linkedin_on):
        page = SessionPage(pages={LOGIN_URL: dict(FEED_DOM)}, redirects={LOGIN_URL: FEED_URL})
        sleep = AsyncMock()
        result = await interactive_login(launcher_for(page), sleep=sleep, clock=lambda: 0.0)
        assert result.state == CONNECTED
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_times_out_after_five_minutes(self, linkedin_on):
        now = [0.0]

        async def sleep(seconds):
            now[0] += seconds

        result = await interactive_login(launcher_for(SessionPage()), poll=30, clock=lambda: now[0], sleep=sleep)
        assert result.state == NEEDS_LOGIN and "5 minutes" in result.detail
        assert now[0] == 300

    @pytest.mark.asyncio
    async def test_disabled(self, linkedin_off):
        launcher = launcher_for(SessionPage())
        assert (await interactive_login(launcher)).state == DISABLED
        assert launcher.calls == []


# ─── Status store and side effects ─────────────────────────


class TestApplyLinkedIn:
    def test_needs_login_stops_linkedin_and_opens_one_item(self, db, linkedin_on):
        assert channel_status.apply_linkedin(db, SessionStatus(NEEDS_LOGIN, detail="login_wall"), NOW) is True
        assert setting(db, DISABLED_KEY) == "true"
        assert setting(db, DISABLED_REASON_KEY) == "needs_login"
        assert setting(db, "global_pause") is None  # only LinkedIn stops
        items = reconnect_items(db)
        assert len(items) == 1
        assert items[0].type == AttentionType.CHANNEL_ERROR and items[0].payload["title"] == "Reconnect LinkedIn"
        # A second failing check neither duplicates the item nor alerts again.
        assert channel_status.apply_linkedin(db, SessionStatus(NEEDS_LOGIN, detail="login_wall"), NOW) is False
        assert len(reconnect_items(db)) == 1
        assert channel_status.linkedin_status(db)["state"] == NEEDS_LOGIN

    def test_connected_lifts_a_needs_login_stop(self, db, linkedin_on):
        channel_status.apply_linkedin(db, SessionStatus(NEEDS_LOGIN), NOW)
        channel_status.apply_linkedin(db, SessionStatus(CONNECTED, "Jane Doe", "https://www.linkedin.com/in/jd"), NOW)
        assert setting(db, DISABLED_KEY) == "false"
        assert all(item.status == AttentionStatus.RESOLVED for item in reconnect_items(db))
        row = channel_status.linkedin_status(db)
        assert row["state"] == CONNECTED and row["account_name"] == "Jane Doe"
        assert row["last_checked_at"] == NOW.isoformat()

    def test_connected_keeps_a_captcha_stop(self, db, linkedin_on):
        db.add_all([Setting(key=DISABLED_KEY, value="true"), Setting(key=DISABLED_REASON_KEY, value="captcha")])
        db.commit()
        channel_status.apply_linkedin(db, SessionStatus(CONNECTED, "Jane Doe"), NOW)
        assert setting(db, DISABLED_KEY) == "true"
        row = channel_status.linkedin_status(db)
        assert row["state"] == DISABLED and "captcha" in row["detail"]

    def test_error_keeps_the_known_account(self, db, linkedin_on):
        channel_status.apply_linkedin(db, SessionStatus(CONNECTED, "Jane Doe", "https://www.linkedin.com/in/jd"), NOW)
        channel_status.apply_linkedin(db, SessionStatus(ERROR, detail="TimeoutError"), NOW)
        row = channel_status.linkedin_status(db)
        assert (row["state"], row["account_name"], row["detail"]) == (ERROR, "Jane Doe", "TimeoutError")
        assert setting(db, DISABLED_KEY) is None

    def test_stores_only_status_fields(self, db, linkedin_on):
        channel_status.apply_linkedin(db, SessionStatus(CONNECTED, "Jane Doe", "https://www.linkedin.com/in/jd"), NOW)
        stored = json.loads(setting(db, "channel_status:linkedin"))
        assert set(stored) == {"state", "account_name", "profile_url", "detail", "last_checked_at"}

    def test_linkedin_enabled_false_wins(self, db, linkedin_off):
        channel_status.apply_linkedin(db, SessionStatus(CONNECTED, "Jane Doe"), NOW)
        row = channel_status.linkedin_status(db)
        assert row["state"] == DISABLED and "LINKEDIN_ENABLED" in row["detail"]

    def test_unknown_before_the_first_check(self, db, linkedin_on):
        row = channel_status.linkedin_status(db)
        assert row["state"] == "unknown" and row["last_checked_at"] is None

    @pytest.mark.asyncio
    async def test_record_alerts_the_owner_once(self, db, linkedin_on):
        from app.db import SessionLocal

        notify = AsyncMock()
        await channel_status.record_linkedin(SessionStatus(NEEDS_LOGIN, detail="checkpoint"), SessionLocal, notify)
        await channel_status.record_linkedin(SessionStatus(NEEDS_LOGIN, detail="checkpoint"), SessionLocal, notify)
        notify.assert_awaited_once()
        assert "Reconnect LinkedIn" in notify.await_args.args[0]


class TestPreflight:
    @pytest.fixture
    def session_factory(self, db):
        from app.db import SessionLocal

        return SessionLocal

    def seed(self, db, state, checked_at):
        channel_status.save_status(db, "linkedin", SessionStatus(state, "Jane Doe"), checked_at)
        db.commit()

    @pytest.mark.asyncio
    async def test_recent_good_check_is_trusted(self, db, session_factory, linkedin_on):
        self.seed(db, CONNECTED, NOW - timedelta(minutes=10))
        checker = AsyncMock()
        assert await channel_status.linkedin_preflight(checker, session_factory, now=NOW) == CONNECTED
        checker.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_check_runs_again(self, db, session_factory, linkedin_on):
        self.seed(db, CONNECTED, NOW - timedelta(minutes=45))
        checker = AsyncMock(return_value=SessionStatus(NEEDS_LOGIN, detail="login_wall"))
        notify = AsyncMock()
        assert await channel_status.linkedin_preflight(checker, session_factory, notify, now=NOW) == NEEDS_LOGIN
        checker.assert_awaited_once()
        assert setting(db, DISABLED_KEY) == "true"
        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_batch_checks(self, db, session_factory, linkedin_on):
        checker = AsyncMock(return_value=SessionStatus(CONNECTED, "Jane Doe"))
        assert await channel_status.linkedin_preflight(checker, session_factory, now=NOW) == CONNECTED
        checker.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stopped_or_switched_off_never_checks(self, db, session_factory, monkeypatch):
        checker = AsyncMock()
        monkeypatch.setattr(settings, "linkedin_enabled", False)
        assert await channel_status.linkedin_preflight(checker, session_factory, now=NOW) == DISABLED
        monkeypatch.setattr(settings, "linkedin_enabled", True)
        db.add_all([Setting(key=DISABLED_KEY, value="true"), Setting(key=DISABLED_REASON_KEY, value="needs_login")])
        db.commit()
        assert await channel_status.linkedin_preflight(checker, session_factory, now=NOW) == NEEDS_LOGIN
        checker.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recent_error_is_not_retried_every_batch(self, db, session_factory, linkedin_on):
        self.seed(db, ERROR, NOW - timedelta(minutes=5))
        checker = AsyncMock()
        assert await channel_status.linkedin_preflight(checker, session_factory, now=NOW) == ERROR
        checker.assert_not_awaited()


# ─── Outlook and WhatsApp ──────────────────────────────────


class TestOtherChannels:
    @pytest.mark.asyncio
    async def test_whatsapp_reads_the_business_name(self):
        def handler(request):
            assert request.url.path.endswith(f"/{settings.whatsapp_phone_number_id}")
            return httpx.Response(200, json={"verified_name": "AI ConnectX", "display_phone_number": "+91 90000 00000"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await channel_status.check_whatsapp(http)
        assert result == SessionStatus(CONNECTED, "AI ConnectX (+91 90000 00000)")

    @pytest.mark.parametrize(
        ("code", "body", "state"),
        [
            (401, {}, NEEDS_LOGIN),
            (400, {"error": {"code": 190, "message": "expired"}}, NEEDS_LOGIN),
            (500, {"error": {"code": 2}}, ERROR),
        ],
    )
    @pytest.mark.asyncio
    async def test_whatsapp_failures(self, code, body, state):
        transport = httpx.MockTransport(lambda request: httpx.Response(code, json=body))
        async with httpx.AsyncClient(transport=transport) as http:
            assert (await channel_status.check_whatsapp(http)).state == state

    def test_whatsapp_not_configured(self, db, monkeypatch):
        monkeypatch.setattr(settings, "whatsapp_token", "")
        assert channel_status.whatsapp_status(db)["state"] == ERROR

    def test_outlook_without_a_sign_in_needs_login(self, db, tmp_path, monkeypatch):
        monkeypatch.setenv("MS_TOKEN_CACHE_PATH", str(tmp_path / "missing.json"))
        assert channel_status.outlook_status(db)["state"] == NEEDS_LOGIN
        db.add(Setting(key="outlook_disabled", value="true"))
        db.commit()
        assert channel_status.outlook_status(db)["state"] == DISABLED

    def test_outlook_silent_check_reports_the_account(self, tmp_path, monkeypatch):
        from app.channels import outlook

        cache = tmp_path / "ms.json"
        cache.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("MS_TOKEN_CACHE_PATH", str(cache))
        app = SimpleNamespace(
            get_accounts=lambda: [{"username": "owner@uni.example"}],
            acquire_token_silent=lambda scopes, account: {"access_token": "secret"},
        )
        fake_tokens = SimpleNamespace(app=app, _save_cache=lambda: None)
        monkeypatch.setattr(outlook, "TokenProvider", lambda *a: fake_tokens)
        assert channel_status._outlook_silent() == SessionStatus(CONNECTED, "owner@uni.example")
        app.acquire_token_silent = lambda scopes, account: None
        assert channel_status._outlook_silent().state == NEEDS_LOGIN


# ─── LinkedIn client and scheduler honour LINKEDIN_ENABLED ─


class TestEnabledFlag:
    @pytest.mark.asyncio
    async def test_client_is_unavailable_when_switched_off(self, linkedin_off):
        h = make_client(store=FakeStore())
        assert h.client.available is False
        with pytest.raises(LinkedInUnavailable):
            await h.client.is_connected("https://www.linkedin.com/in/x")
        assert h.launches == []

    def test_scheduler_skips_linkedin_jobs_when_switched_off(self, linkedin_off):
        assert scheduler.JobMonitor(session_factory=lambda: None).is_channel_disabled("linkedin") is True

    @pytest.mark.asyncio
    async def test_session_job_escalates_errors(self, monkeypatch):
        from app.channels import status as module

        monkeypatch.setattr(module, "check_linkedin", AsyncMock(return_value=SessionStatus(ERROR, detail="boom")))
        with pytest.raises(RuntimeError, match="boom"):
            await scheduler.run_linkedin_session()
        monkeypatch.setattr(module, "check_linkedin", AsyncMock(return_value=SessionStatus(NEEDS_LOGIN)))
        assert await scheduler.run_linkedin_session() == NEEDS_LOGIN

    @pytest.mark.asyncio
    async def test_linkedin_batches_check_the_session_first(self, monkeypatch):
        order = []

        async def preflight():
            order.append("preflight")
            return CONNECTED

        async def in_session(work):
            order.append("batch")

        monkeypatch.setattr(scheduler, "linkedin_preflight", preflight)
        monkeypatch.setattr(scheduler, "in_session", in_session)
        await scheduler.run_send_queue(True)
        await scheduler.run_connection_poll()
        await scheduler.run_replies()
        assert order == ["preflight", "batch"] * 3


# ─── API ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    from argon2 import PasswordHasher

    monkeypatch.setattr(security, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    security._dummy_hash.cache_clear()
    yield
    security._dummy_hash.cache_clear()


@pytest.fixture
def app(db):
    application = FastAPI()
    oauth.install_session_middleware(application)
    application.include_router(api_router)
    application.dependency_overrides[get_session] = lambda: db
    return application


def client_as(app, db, role: UserRole) -> TestClient:
    user = User(
        email=f"{role.value}@example.test", name=role.value, role=role,
        provider=AuthProvider.PASSWORD, password_hash=security.hash_password("correct horse battery"),
    )
    db.add(user)
    db.commit()
    http = TestClient(app, follow_redirects=False, headers=CSRF)
    token, _ = security.create_session_token(user)
    http.cookies.set(security.SESSION_COOKIE, token)
    return http


@pytest.fixture
def fake_check(monkeypatch):
    holder = {"result": SessionStatus(CONNECTED, "Jane Doe", "https://www.linkedin.com/in/jd")}

    async def check():
        return holder["result"]

    monkeypatch.setattr(linkedin_session, "check_session", check)
    monkeypatch.setattr(channel_status, "alert_reconnect", AsyncMock())
    return holder


class TestChannelsApi:
    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.MEMBER])
    def test_admin_only(self, app, db, role):
        http = client_as(app, db, role)
        assert http.get("/api/channels/status").status_code == 403
        assert http.post("/api/channels/linkedin/check").status_code == 403
        assert http.post("/api/channels/linkedin/connect").status_code == 403

    def test_requires_sign_in_and_csrf(self, app, db):
        assert TestClient(app).get("/api/channels/status").status_code == 401
        http = client_as(app, db, UserRole.ADMIN)
        http.headers.pop(security.CSRF_HEADER)
        assert http.post("/api/channels/linkedin/check").status_code == 403

    def test_status_lists_all_three_channels(self, app, db, linkedin_on):
        body = client_as(app, db, UserRole.ADMIN).get("/api/channels/status").json()
        assert [c["channel"] for c in body["channels"]] == ["linkedin", "outlook", "whatsapp"]
        assert {"state", "account_name", "last_checked_at"} <= set(body["channels"][0])
        assert body["login_command"] == "python -m app.cli linkedin-login"
        assert body["linkedin_login_running"] is False

    def test_check_now_stores_the_account(self, app, db, linkedin_on, fake_check):
        row = client_as(app, db, UserRole.ADMIN).post("/api/channels/linkedin/check").json()
        assert (row["state"], row["account_name"]) == (CONNECTED, "Jane Doe")
        assert row["last_checked_at"]

    def test_check_now_needs_login_raises_the_banner(self, app, db, linkedin_on, fake_check):
        fake_check["result"] = SessionStatus(NEEDS_LOGIN, detail="login_wall")
        http = client_as(app, db, UserRole.ADMIN)
        assert http.post("/api/channels/linkedin/check").json()["state"] == NEEDS_LOGIN
        channel_status.alert_reconnect.assert_awaited_once()
        assert len(reconnect_items(db)) == 1
        status = http.get("/api/dashboard/status").json()
        assert status["linkedin_needs_login"] is True
        assert all(c["channel"] != "linkedin" for c in status["disabled_channels"])  # its own banner instead

    def test_banner_flag_is_visible_to_viewers(self, app, db, linkedin_on):
        channel_status.apply_linkedin(db, SessionStatus(NEEDS_LOGIN), NOW)
        assert client_as(app, db, UserRole.VIEWER).get("/api/dashboard/status").json()["linkedin_needs_login"] is True

    def test_check_other_channels(self, app, db, monkeypatch):
        monkeypatch.setattr(channel_status, "check_whatsapp", AsyncMock(return_value=SessionStatus(CONNECTED, "Biz")))
        http = client_as(app, db, UserRole.ADMIN)
        row = http.post("/api/channels/whatsapp/check").json()
        assert (row["channel"], row["state"], row["account_name"]) == ("whatsapp", CONNECTED, "Biz")
        assert http.post("/api/channels/telegram/check").status_code == 404

    def test_connect_without_a_display_explains_the_cli(self, app, db, linkedin_on, monkeypatch):
        monkeypatch.setattr(channels_api, "has_display", lambda: False)
        response = client_as(app, db, UserRole.ADMIN).post("/api/channels/linkedin/connect")
        assert response.status_code == 409
        assert "python -m app.cli linkedin-login" in response.json()["detail"]

    def test_connect_refused_when_switched_off(self, app, db, linkedin_off, monkeypatch):
        monkeypatch.setattr(channels_api, "has_display", lambda: True)
        response = client_as(app, db, UserRole.ADMIN).post("/api/channels/linkedin/connect")
        assert response.status_code == 409 and "LINKEDIN_ENABLED" in response.json()["detail"]

    def test_connect_opens_the_login_and_records_it(self, app, db, linkedin_on, monkeypatch):
        monkeypatch.setattr(channels_api, "has_display", lambda: True)
        started = []

        async def login():
            started.append(1)
            return SessionStatus(CONNECTED, "Jane Doe", "https://www.linkedin.com/in/jd")

        monkeypatch.setattr(linkedin_session, "interactive_login", login)
        with client_as(app, db, UserRole.ADMIN) as http:
            response = http.post("/api/channels/linkedin/connect")
            assert response.status_code == 202 and "browser window" in response.json()["message"]
            deadline = time.monotonic() + 5
            while channels_api.login_running() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not channels_api.login_running()
            row = http.get("/api/channels/status").json()["channels"][0]
        assert started == [1]
        assert (row["state"], row["account_name"]) == (CONNECTED, "Jane Doe")

    def test_only_one_login_window(self, app, db, linkedin_on, monkeypatch):
        monkeypatch.setattr(channels_api, "has_display", lambda: True)
        monkeypatch.setattr(channels_api, "login_running", lambda: True)
        http = client_as(app, db, UserRole.ADMIN)
        assert http.post("/api/channels/linkedin/connect").status_code == 409
        assert http.post("/api/channels/linkedin/check").status_code == 409

    def test_has_display(self, monkeypatch):
        monkeypatch.setattr(channels_api.sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert channels_api.has_display() is False
        monkeypatch.setenv("DISPLAY", ":0")
        assert channels_api.has_display() is True


# ─── CLI ───────────────────────────────────────────────────


class TestCli:
    @pytest.mark.asyncio
    async def test_login_prints_the_account_and_stores_status(self, db, linkedin_on, capsys):
        login = AsyncMock(return_value=SessionStatus(CONNECTED, "Jane Doe", "https://www.linkedin.com/in/jd"))
        assert await cli.run_linkedin_login(login) == 0
        assert "connected as Jane Doe" in capsys.readouterr().out
        assert json.loads(setting(db, "channel_status:linkedin"))["account_name"] == "Jane Doe"

    @pytest.mark.asyncio
    async def test_login_timeout_fails(self, db, linkedin_on, capsys, monkeypatch):
        monkeypatch.setattr(channel_status, "alert_reconnect", AsyncMock())
        login = AsyncMock(return_value=SessionStatus(NEEDS_LOGIN, detail="Timed out after 5 minutes waiting for the feed"))
        assert await cli.run_linkedin_login(login) == 1
        assert "Timed out" in capsys.readouterr().err

    def test_command_is_registered(self, monkeypatch):
        seen = []

        async def fake_login(login=None):
            seen.append(1)
            return 0

        monkeypatch.setattr(cli, "run_linkedin_login", fake_login)
        assert cli.main(["linkedin-login"]) == 0 and seen == [1]
