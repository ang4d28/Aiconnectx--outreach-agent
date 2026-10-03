from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels import linkedin
from app.channels.base import Channel, Thread
from app.channels.linkedin import (
    SELECTORS,
    DbStore,
    LinkedInActionFailed,
    LinkedInBlocked,
    LinkedInClient,
    LinkedInUnavailable,
    blocked_reason,
    normalize_profile_url,
    parse_list_time,
    reply_id,
)
from app.db.models import AttentionItem, AttentionType, ChannelType, MessageStatus, OutreachState, Setting

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # Mon 11:30 IST
TODAY = date(2026, 10, 5)
PROFILE = "https://www.linkedin.com/in/jane-doe/"
S = SELECTORS


# ─── Fake Playwright page ──────────────────────────────────


class FakeLocator:
    def __init__(self, page, key):
        self.page = page
        self.key = key

    @property
    def first(self):
        return self

    def nth(self, index):
        return FakeLocator(self.page, (self.key, index))

    def locator(self, child):
        base = self.key if isinstance(self.key, tuple) else (self.key,)
        return FakeLocator(self.page, (*base, child))

    async def count(self):
        value = self.page.dom.get(self.key)
        if value is None:
            return 0
        if isinstance(value, list):
            return len(value)
        return value if isinstance(value, int) and not isinstance(value, bool) else 1

    async def click(self):
        self.page.clicks.append(self.key)
        self.page.focus = self.key
        handler = self.page.on_click.get(self.key)
        if handler:
            handler(self.page)

    async def inner_text(self):
        value = self.page.dom.get(self.key)
        return value if isinstance(value, str) else ""

    async def all_inner_texts(self):
        value = self.page.dom.get(self.key)
        return list(value) if isinstance(value, list) else []

    async def get_attribute(self, name):
        return self.page.attrs.get((self.key, name))


class FakePage:
    def __init__(self, pages=None, redirects=None):
        self.pages = pages or {}  # url -> dom dict shown after goto
        self.redirects = redirects or {}
        self.dom: dict = {}
        self.attrs: dict = {}
        self.on_click: dict = {}
        self.clicks: list = []
        self.visits: list = []
        self.typed: dict = {}
        self.focus = None
        self.url = "about:blank"
        self.wheel = []
        self.mouse = SimpleNamespace(wheel=self._wheel)
        self.keyboard = SimpleNamespace(type=self._type)

    async def goto(self, url, wait_until=None):
        self.visits.append(url)
        self.url = self.redirects.get(url, url)
        self.dom = dict(self.pages.get(url, {}))

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def _wheel(self, dx, dy):
        self.wheel.append(dy)

    async def _type(self, char):
        self.typed[self.focus] = self.typed.get(self.focus, "") + char


class FakeStore:
    def __init__(self, disabled=False, actions=0):
        self.disabled = disabled
        self.counts = {TODAY: actions}
        self.blocks: list = []

    def is_disabled(self):
        return self.disabled

    def actions_on(self, day):
        return self.counts.get(day, 0)

    def increment(self, day):
        self.counts[day] = self.counts.get(day, 0) + 1

    def disable_and_pause(self, reason, url):
        self.disabled = True
        self.blocks.append((reason, url))


def make_client(page=None, store=None, dry_run=False, cap=20):
    page = page or FakePage()
    launches = []

    @asynccontextmanager
    async def launcher():
        launches.append(1)
        yield page

    sleep = AsyncMock()
    notify = AsyncMock()
    client = LinkedInClient(
        store=store or FakeStore(), launcher=launcher, notify=notify, dry_run=dry_run,
        sleep=sleep, rng=random.Random(3), clock=lambda: NOW, daily_cap=cap,
    )
    return SimpleNamespace(client=client, page=page, sleep=sleep, notify=notify,
                           launches=launches, store=client.store)


def profile_page(**dom):
    return FakePage(pages={PROFILE: dom})


def open_dialog(page):
    page.dom.update({S["add_note"]: 1, S["note_box"]: 1, S["send_invite"]: 1})


def step_delays(sleep):
    return [c.args[0] for c in sleep.await_args_list if c.args[0] >= 1]


# ─── Helpers ───────────────────────────────────────────────


class TestHelpers:
    def test_normalize_profile_url(self):
        assert normalize_profile_url("http://linkedin.com/in/Jane-Doe/?trk=x") == "https://www.linkedin.com/in/jane-doe"
        assert normalize_profile_url(PROFILE) == "https://www.linkedin.com/in/jane-doe"

    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            ("https://www.linkedin.com/checkpoint/challenge/abc", "checkpoint"),
            ("https://www.linkedin.com/authwall?trk=1", "login_wall"),
            ("https://www.linkedin.com/login", "login_wall"),
            ("https://www.linkedin.com/uas/login?session", "login_wall"),
            ("https://www.linkedin.com/in/someone-login/", None),
            ("https://www.linkedin.com/messaging/", None),
        ],
    )
    def test_blocked_reason(self, url, reason):
        assert blocked_reason(url) == reason

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2:15 PM", TODAY),
            ("14:05", TODAY),
            ("Yesterday", TODAY - timedelta(days=1)),
            ("Sat", date(2026, 10, 3)),
            ("Mon", date(2026, 9, 28)),
            ("Oct 3", date(2026, 10, 3)),
            ("Dec 30", date(2025, 12, 30)),
            ("Sep 9, 2025", date(2025, 9, 9)),
            ("", None),
            ("gibberish", None),
        ],
    )
    def test_parse_list_time(self, text, expected):
        assert parse_list_time(text, NOW) == expected

    def test_reply_id_stable(self):
        assert reply_id(PROFILE, " Hi ") == reply_id("https://linkedin.com/in/jane-doe", "Hi")
        assert reply_id(PROFILE, "Hi") != reply_id(PROFILE, "Hello")

    def test_implements_channel_protocol(self):
        assert isinstance(make_client().client, Channel)


# ─── Connection notes ──────────────────────────────────────


class TestConnectionNote:
    @pytest.mark.asyncio
    async def test_sends_note_with_human_pacing(self):
        page = profile_page(**{S["connect"]: 1})
        page.on_click[S["connect"]] = open_dialog
        h = make_client(page)
        assert await h.client.send_connection_note(PROFILE, "Hi Jane, short note") is None
        assert page.visits == [PROFILE]
        assert page.clicks == [S["connect"], S["add_note"], S["note_box"], S["send_invite"]]
        assert page.typed[S["note_box"]] == "Hi Jane, short note"
        delays = step_delays(h.sleep)
        assert len(delays) >= 3 and all(5 <= d <= 25 for d in delays)
        keystrokes = [c.args[0] for c in h.sleep.await_args_list if c.args[0] < 1]
        assert len(keystrokes) == len("Hi Jane, short note") and len(set(keystrokes)) > 1
        assert page.wheel  # scrolled
        assert h.store.actions_on(TODAY) == 1

    @pytest.mark.asyncio
    async def test_connect_via_more_menu(self):
        page = profile_page(**{S["more"]: 1})
        page.on_click[S["more"]] = lambda p: p.dom.update({S["more_connect"]: 1})
        page.on_click[S["more_connect"]] = open_dialog
        h = make_client(page)
        await h.client.send_connection_note(PROFILE, "Hi")
        assert page.clicks[:2] == [S["more"], S["more_connect"]]
        assert page.clicks[-1] == S["send_invite"]

    @pytest.mark.asyncio
    async def test_note_over_200_chars_rejected_before_browser(self):
        h = make_client()
        with pytest.raises(ValueError):
            await h.client.send_connection_note(PROFILE, "x" * 201)
        assert h.launches == []

    @pytest.mark.asyncio
    async def test_already_connected_or_pending_does_nothing(self):
        for dom in ({S["first_degree"]: 1}, {S["pending"]: 1}):
            h = make_client(profile_page(**dom))
            await h.client.send_connection_note(PROFILE, "Hi")
            assert h.page.clicks == [] and h.store.actions_on(TODAY) == 0

    @pytest.mark.asyncio
    async def test_note_limit_fails_without_sending(self):
        page = profile_page(**{S["connect"]: 1})
        page.on_click[S["connect"]] = lambda p: p.dom.update({S["note_limit"]: 1, S["add_note"]: 1})
        h = make_client(page)
        with pytest.raises(LinkedInActionFailed):
            await h.client.send_connection_note(PROFILE, "Hi")
        assert S["send_invite"] not in page.clicks
        assert h.store.actions_on(TODAY) == 0

    @pytest.mark.asyncio
    async def test_no_connect_option(self):
        h = make_client(profile_page())
        with pytest.raises(LinkedInActionFailed):
            await h.client.send_connection_note(PROFILE, "Hi")


# ─── Blockers ──────────────────────────────────────────────


class TestBlockers:
    @pytest.mark.asyncio
    async def test_checkpoint_stops_disables_pauses_and_alerts(self):
        page = FakePage(redirects={PROFILE: "https://www.linkedin.com/checkpoint/challenge/x"})
        h = make_client(page)
        with pytest.raises(LinkedInBlocked):
            await h.client.send_dm(PROFILE, "Hello")
        assert h.store.blocks == [("checkpoint", "https://www.linkedin.com/checkpoint/challenge/x")]
        assert page.clicks == [] and page.typed == {}
        assert "checkpoint" in h.notify.await_args.args[0]
        assert h.client.available is False
        assert h.store.actions_on(TODAY) == 0

    @pytest.mark.asyncio
    async def test_captcha_element(self):
        h = make_client(profile_page(**{S["captcha"]: 1, S["connect"]: 1}))
        with pytest.raises(LinkedInBlocked):
            await h.client.send_connection_note(PROFILE, "Hi")
        assert h.store.blocks[0][0] == "captcha"
        assert h.page.clicks == []

    @pytest.mark.asyncio
    async def test_login_wall_during_reads(self):
        h = make_client(FakePage(pages={f"{linkedin.BASE_URL}/messaging/": {S["login_wall"]: 1}}))
        with pytest.raises(LinkedInBlocked):
            await h.client.read_new_replies(NOW - timedelta(hours=1))
        assert h.store.blocks[0][0] == "login_wall"

    @pytest.mark.asyncio
    async def test_block_after_click_stops_flow(self):
        page = profile_page(**{S["message"]: 1})
        page.on_click[S["message"]] = lambda p: setattr(p, "url", "https://www.linkedin.com/checkpoint/x")
        h = make_client(page)
        with pytest.raises(LinkedInBlocked):
            await h.client.send_dm(PROFILE, "Hello")
        assert S["msg_send"] not in page.clicks

    @pytest.mark.asyncio
    async def test_alert_failure_still_blocks(self):
        page = FakePage(redirects={PROFILE: "https://www.linkedin.com/authwall"})
        h = make_client(page)
        h.notify.side_effect = RuntimeError("whatsapp down")
        with pytest.raises(LinkedInBlocked):
            await h.client.is_connected(PROFILE)
        assert h.store.disabled

    @pytest.mark.asyncio
    async def test_disabled_channel_never_opens_browser(self):
        h = make_client(store=FakeStore(disabled=True))
        with pytest.raises(LinkedInUnavailable):
            await h.client.send_dm(PROFILE, "Hi")
        with pytest.raises(LinkedInUnavailable):
            await h.client.read_new_replies(NOW)
        assert h.launches == []


# ─── Cap, DRY_RUN, session ─────────────────────────────────


class TestGuards:
    @pytest.mark.asyncio
    async def test_shared_daily_cap(self):
        h = make_client(store=FakeStore(actions=20), cap=20)
        assert h.client.remaining_today() == 0
        with pytest.raises(LinkedInUnavailable):
            await h.client.comment_on_post("https://www.linkedin.com/posts/1", "Great post")
        assert h.launches == []

    @pytest.mark.asyncio
    async def test_all_write_actions_share_the_counter(self):
        post = "https://www.linkedin.com/posts/1"
        page = FakePage(pages={post: {S["comment_button"]: 1}, PROFILE: {S["message"]: 1}})
        page.on_click[S["comment_button"]] = lambda p: p.dom.update({S["comment_box"]: 1, S["comment_submit"]: 1})
        page.on_click[S["message"]] = lambda p: p.dom.update({S["msg_box"]: 1, S["msg_send"]: 1})
        h = make_client(page, store=FakeStore(actions=18), cap=20)
        await h.client.comment_on_post(post, "Congrats")
        await h.client.send_dm(PROFILE, "Hi")
        assert h.client.remaining_today() == 0
        with pytest.raises(LinkedInUnavailable):
            await h.client.send_dm(PROFILE, "Again")

    @pytest.mark.asyncio
    async def test_dry_run_skips_writes_but_reads(self):
        page = FakePage(pages={PROFILE: {S["first_degree"]: 1}})
        h = make_client(page, dry_run=True)
        assert await h.client.send_dm(PROFILE, "Hi") is None
        assert await h.client.comment_on_post("https://x", "y") is None
        assert await h.client.send_connection_note(PROFILE, "Hi") is None
        assert h.launches == []
        assert await h.client.is_connected(PROFILE) is True
        assert h.store.actions_on(TODAY) == 0

    @pytest.mark.asyncio
    async def test_one_session_at_a_time(self):
        active, peak = 0, 0

        @asynccontextmanager
        async def launcher():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            try:
                yield FakePage(pages={PROFILE: {S["first_degree"]: 1}})
            finally:
                active -= 1

        async def tick(_):
            await asyncio.sleep(0)

        client = LinkedInClient(store=FakeStore(), launcher=launcher, notify=AsyncMock(),
                                dry_run=False, sleep=tick, rng=random.Random(1), clock=lambda: NOW)
        await asyncio.gather(*(client.is_connected(PROFILE) for _ in range(3)))
        assert peak == 1


# ─── Messaging, connection checks, comments ────────────────


class TestMessaging:
    @pytest.mark.asyncio
    async def test_send_dm_returns_profile_thread_id(self):
        page = profile_page(**{S["message"]: 1})
        page.on_click[S["message"]] = lambda p: p.dom.update({S["msg_box"]: 1, S["msg_send"]: 1})
        h = make_client(page)
        assert await h.client.send_dm(PROFILE, "Hello Jane") == "https://www.linkedin.com/in/jane-doe"
        assert page.typed[S["msg_box"]] == "Hello Jane"
        assert page.clicks[-1] == S["msg_send"]
        assert h.store.actions_on(TODAY) == 1

    @pytest.mark.asyncio
    async def test_reply_and_send_message_use_dm(self):
        h = make_client()
        h.client.send_dm = AsyncMock(return_value="t")
        await h.client.reply(Thread(recipient=PROFILE, thread_id="t"), "x")
        await h.client.send_message(Thread(recipient=PROFILE), "y")
        assert [c.args for c in h.client.send_dm.await_args_list] == [(PROFILE, "x"), (PROFILE, "y")]

    @pytest.mark.asyncio
    async def test_dm_without_message_button(self):
        h = make_client(profile_page())
        with pytest.raises(LinkedInActionFailed):
            await h.client.send_dm(PROFILE, "Hi")

    @pytest.mark.asyncio
    async def test_is_connected(self):
        assert await make_client(profile_page(**{S["first_degree"]: 1})).client.is_connected(PROFILE) is True
        assert await make_client(profile_page(**{S["pending"]: 1})).client.is_connected(PROFILE) is False

    @pytest.mark.asyncio
    async def test_comment_on_post(self):
        post = "https://www.linkedin.com/posts/acme-hiring-1"
        page = FakePage(pages={post: {S["comment_button"]: 1}})
        page.on_click[S["comment_button"]] = lambda p: p.dom.update({S["comment_box"]: 1, S["comment_submit"]: 1})
        h = make_client(page)
        await h.client.comment_on_post(post, "Congrats on the growth!")
        assert page.typed[S["comment_box"]] == "Congrats on the growth!"
        assert page.clicks[-1] == S["comment_submit"]

    @pytest.mark.asyncio
    async def test_read_new_replies(self):
        conv = S["conversations"]
        inbox = {
            conv: 4,
            (conv, 0, S["conv_time"]): "11:02 AM", (conv, 0, S["conv_snippet"]): "Jane: Sounds great",
            (conv, 1, S["conv_time"]): "10:15 AM", (conv, 1, S["conv_snippet"]): "You: Thanks!",
            (conv, 2, S["conv_time"]): "Yesterday", (conv, 2, S["conv_snippet"]): "Raj: Not now",
            (conv, 3, S["conv_time"]): "Sep 1", (conv, 3, S["conv_snippet"]): "Old: hello",
        }
        page = FakePage(pages={f"{linkedin.BASE_URL}/messaging/": inbox})
        threads = {
            0: ("/in/Jane-Doe/", ["Hi Jane, AI ConnectX here", "Sounds great, let's talk"]),
            2: ("https://www.linkedin.com/in/raj/", ["Not now, maybe in March"]),
        }

        def opener(index):
            def open_thread(p):
                href, bodies = threads[index]
                p.attrs[(S["thread_profile"], "href")] = href
                p.dom[S["thread_profile"]] = 1
                p.dom[S["thread_messages"]] = bodies
            return open_thread

        for index in threads:
            page.on_click[(conv, index, S["conv_link"])] = opener(index)
        h = make_client(page)
        replies = await h.client.read_new_replies(NOW - timedelta(days=1, hours=2))
        assert [r.text for r in replies] == ["Sounds great, let's talk", "Not now, maybe in March"]
        assert replies[0].thread_id == replies[0].sender == "https://www.linkedin.com/in/jane-doe"
        assert replies[0].message_id == reply_id(PROFILE, "Sounds great, let's talk")
        assert (conv, 1, S["conv_link"]) not in page.clicks  # our own last message
        assert (conv, 3, S["conv_link"]) not in page.clicks  # older than since
        assert h.store.actions_on(TODAY) == 0  # reads do not use the cap


# ─── DB store and Playwright launcher ──────────────────────


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    def get(self, model, key):
        return self.rows.get(key) if model is Setting else None

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, Setting):
            self.rows[obj.key] = obj

    def commit(self):
        pass

    def close(self):
        pass


class TestDbStore:
    def test_disable_pause_and_attention(self):
        rows: dict = {}
        sessions: list[FakeSession] = []

        def factory():
            sessions.append(FakeSession(rows))
            return sessions[-1]

        store = DbStore(factory)
        assert store.is_disabled() is False
        store.disable_and_pause("captcha", "https://x")
        assert rows["linkedin_disabled"].value == "true"
        assert rows["global_pause"].value == "true"
        assert rows["linkedin_disabled_reason"].value == "captcha"
        items = [o for s in sessions for o in s.added if isinstance(o, AttentionItem)]
        assert items[0].type == AttentionType.CHANNEL_ERROR and items[0].payload["channel"] == "linkedin"
        assert store.is_disabled() is True
        store.set_enabled()
        assert store.is_disabled() is False

    def test_action_counter_per_day(self):
        rows: dict = {}
        store = DbStore(lambda: FakeSession(rows))
        store.increment(TODAY)
        store.increment(TODAY)
        assert store.actions_on(TODAY) == 2
        assert store.actions_on(TODAY + timedelta(days=1)) == 0


class TestPlaywrightLauncher:
    @pytest.mark.asyncio
    async def test_persistent_profile(self, monkeypatch):
        import playwright.async_api as pw_api

        page = object()
        context = MagicMock()
        context.pages = [page]
        context.close = AsyncMock()
        chromium = MagicMock()
        chromium.launch_persistent_context = AsyncMock(return_value=context)

        @asynccontextmanager
        async def fake_async_playwright():
            yield SimpleNamespace(chromium=chromium)

        monkeypatch.setattr(pw_api, "async_playwright", fake_async_playwright)
        monkeypatch.setattr(linkedin.settings, "linkedin_profile_dir", "/data/li-profile")
        async with linkedin.playwright_page(headless=True) as yielded:
            assert yielded is page
        args, kwargs = chromium.launch_persistent_context.await_args
        assert args[0] == "/data/li-profile" and kwargs["headless"] is True
        context.close.assert_awaited_once()


# ─── Integration with the send queue and reply worker ──────


class TestQueueIntegration:
    def harness(self):
        from tests.test_send_queue import QueueHarness, make_contact, make_message, make_outreach

        outreach = make_outreach(1, channel=ChannelType.LINKEDIN)
        contact = make_contact(1, email=None, linkedin=PROFILE)
        message = make_message(1, 1)
        return QueueHarness([message], {1: outreach}, {1: contact}), message

    @pytest.mark.asyncio
    async def test_block_stops_run_and_keeps_message(self):
        h, message = self.harness()
        h.linkedin.send_connection_note = AsyncMock(side_effect=LinkedInBlocked("captcha"))
        report = await h.run()
        assert report.stopped == "paused"
        assert message.status == MessageStatus.APPROVED

    @pytest.mark.asyncio
    async def test_action_failure_goes_to_needs_manual(self):
        h, message = self.harness()
        h.linkedin.send_connection_note = AsyncMock(side_effect=LinkedInActionFailed("note limit"))
        report = await h.run()
        assert report.needs_manual == 1
        assert message.status == MessageStatus.FAILED
        assert "note limit" in h.db.add.call_args.args[0].payload["reason"]

    @pytest.mark.asyncio
    async def test_capacity_uses_shared_linkedin_counter(self):
        h, message = self.harness()
        h.linkedin.remaining_today = MagicMock(return_value=0)
        report = await h.run()
        assert report.stopped == "daily_cap"
        h.linkedin.send_connection_note.assert_not_called()


class TestReplyIntegration:
    def worker(self, linkedin_client, pending=None):
        from app.db.models import Company, Contact
        from app.workers.replies import ReplyWorker

        contact = SimpleNamespace(id=2, company_id=3, name="Jane Doe", linkedin_url=PROFILE)
        objects = {(Contact, 2): contact, (Company, 3): SimpleNamespace(id=3, name="Acme")}
        db = MagicMock()
        db.get.side_effect = lambda model, key: objects.get((model, key))
        outlook = MagicMock()
        outlook.read_new_replies = AsyncMock(return_value=[])
        worker = ReplyWorker(db=db, channels={ChannelType.LINKEDIN: linkedin_client, ChannelType.EMAIL: outlook},
                             llm=MagicMock(), notify=AsyncMock(), clock=lambda: NOW)
        settings_map: dict = {}
        worker._setting = settings_map.get
        worker._set_setting = settings_map.__setitem__
        worker._pending_connects = lambda: pending or []
        return worker, db

    @pytest.mark.asyncio
    async def test_accepted_connection_queues_first_dm(self):
        accepted = SimpleNamespace(id=1, contact_id=2, state=OutreachState.CONNECT_SENT, updated_at=None)
        client = MagicMock()
        client.available = True
        client.read_new_replies = AsyncMock(return_value=[])
        client.is_connected = AsyncMock(return_value=True)
        worker, db = self.worker(client, [accepted])
        report = await worker.run_once()
        assert report.connected == 1
        assert accepted.state == OutreachState.CONNECTED
        message = db.add.call_args.args[0]
        assert message.outreach_id == 1 and message.status == MessageStatus.APPROVED
        assert "Thanks for connecting, Jane" in message.content

    @pytest.mark.asyncio
    async def test_not_accepted_stays_pending(self):
        pending = SimpleNamespace(id=1, contact_id=2, state=OutreachState.CONNECT_SENT, updated_at=None)
        client = MagicMock(available=True)
        client.read_new_replies = AsyncMock(return_value=[])
        client.is_connected = AsyncMock(return_value=False)
        worker, db = self.worker(client, [pending])
        await worker.run_once()
        assert pending.state == OutreachState.CONNECT_SENT
        assert pending.updated_at == NOW
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_block_while_polling_adds_no_duplicate_attention(self):
        client = MagicMock(available=True)
        client.read_new_replies = AsyncMock(side_effect=LinkedInBlocked("checkpoint"))
        client.is_connected = AsyncMock(side_effect=LinkedInUnavailable("disabled"))
        pending = SimpleNamespace(id=1, contact_id=2, state=OutreachState.CONNECT_SENT, updated_at=None)
        worker, db = self.worker(client, [pending])
        report = await worker.run_once()
        assert report.errors == 0
        assert not [c for c in db.add.call_args_list if isinstance(c.args[0], AttentionItem)]

    @pytest.mark.asyncio
    async def test_disabled_channel_not_polled(self):
        client = MagicMock(available=False)
        client.read_new_replies = AsyncMock()
        worker, _ = self.worker(client)
        await worker.run_once()
        client.read_new_replies.assert_not_called()
