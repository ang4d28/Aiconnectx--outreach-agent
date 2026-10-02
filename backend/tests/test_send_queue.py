from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.channels import outlook
from app.channels.outlook import (
    UNSUBSCRIBE_LINE,
    GraphError,
    OutlookClient,
    TokenProvider,
    retry_after_seconds,
    with_unsubscribe,
)
from app.db.models import MessageStatus, OutreachState
from app.workers import send_queue
from app.workers.send_queue import (
    SendQueue,
    email_daily_cap,
    in_send_window,
    next_follow_up,
    on_dnc_list,
    parse_dnc,
    touched_recently,
)

IN_WINDOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # 11:30 IST


# ─── Guardrail helpers ─────────────────────────────────────


class TestGuardrailHelpers:
    @pytest.mark.parametrize(
        ("utc_hour", "utc_minute", "expected"),
        [(4, 30, True), (4, 29, False), (11, 29, True), (11, 30, False)],
    )
    def test_send_window_ist(self, utc_hour, utc_minute, expected):
        now = datetime(2026, 10, 5, utc_hour, utc_minute, tzinfo=timezone.utc)
        assert in_send_window(now) is expected

    def test_cap_warmup_then_full(self):
        assert email_daily_cap(None, IN_WINDOW) == 20
        assert email_daily_cap(IN_WINDOW - timedelta(days=13), IN_WINDOW) == 20
        assert email_daily_cap(IN_WINDOW - timedelta(days=14), IN_WINDOW) == 40

    def test_touch_rule(self):
        assert touched_recently(None, IN_WINDOW) is False
        assert touched_recently(IN_WINDOW - timedelta(days=13), IN_WINDOW) is True
        assert touched_recently(IN_WINDOW - timedelta(days=14), IN_WINDOW) is False

    def test_touch_rule_naive_datetime(self):
        naive = (IN_WINDOW - timedelta(days=1)).replace(tzinfo=None)
        assert touched_recently(naive, IN_WINDOW) is True

    def test_dnc_matches_email_and_domain(self):
        dnc = parse_dnc("Boss@Acme.com\n@blocked.io, other.org")
        assert on_dnc_list("boss@acme.com", dnc)
        assert on_dnc_list("anyone@blocked.io", dnc)
        assert on_dnc_list("x@other.org", dnc)
        assert not on_dnc_list("hr@acme.com", dnc)

    def test_follow_up_schedule(self):
        assert next_follow_up(IN_WINDOW, 0) == IN_WINDOW + timedelta(days=4)
        assert next_follow_up(IN_WINDOW, 1) == IN_WINDOW + timedelta(days=10)
        assert next_follow_up(IN_WINDOW, 2) is None


# ─── Send queue ────────────────────────────────────────────


def make_message(msg_id: int, outreach_id: int):
    return SimpleNamespace(
        id=msg_id, outreach_id=outreach_id, content="Hello", status=MessageStatus.APPROVED
    )


def make_outreach(outreach_id: int, state=OutreachState.DRAFTED):
    return SimpleNamespace(
        id=outreach_id, contact_id=outreach_id, state=state,
        sent_at=None, follow_up_count=0, follow_up_due=None,
    )


def make_contact(contact_id: int, email="hr@acme.com", **kw):
    return SimpleNamespace(
        id=contact_id, company_id=1, email=email,
        is_unsubscribed=kw.get("unsub", False), last_touch_at=kw.get("last_touch"),
    )


class QueueHarness:
    def __init__(self, messages, outreaches, contacts, settings_map=None, **kw):
        self.client = MagicMock()
        self.client.send_email = AsyncMock(return_value="conv-1")
        self.sleep = AsyncMock()
        self.settings_map = settings_map or {}
        self.queue = SendQueue(
            db=MagicMock(),
            client=self.client,
            dry_run=kw.get("dry_run", False),
            sleep=self.sleep,
            clock=lambda: kw.get("now", IN_WINDOW),
            rng=random.Random(1),
        )
        q = self.queue
        q._approved_email_messages = lambda: messages
        q._setting = lambda key: self.settings_map.get(key)
        q._set_setting = lambda key, value: self.settings_map.__setitem__(key, value)
        q._sent_today = lambda now: kw.get("sent_today", 0)
        q._first_send_at = lambda: kw.get("first_send_at")
        q._company_unsubscribed = lambda company_id: kw.get("company_unsub", False)
        repo = send_queue.repository
        self.patches = [
            patch.object(repo, "get_outreach", side_effect=lambda db, i: outreaches[i]),
            patch.object(repo, "get_contact", side_effect=lambda db, i: contacts[i]),
            patch.object(repo, "get_company", return_value=SimpleNamespace(name="Acme")),
        ]

    async def run(self):
        for p in self.patches:
            p.start()
        try:
            return await self.queue.run_once()
        finally:
            for p in self.patches:
                p.stop()


def simple_setup(count: int, **kw):
    messages = [make_message(i, i) for i in range(1, count + 1)]
    outreaches = {i: make_outreach(i) for i in range(1, count + 1)}
    contacts = {i: make_contact(i, email=f"p{i}@acme.com") for i in range(1, count + 1)}
    return QueueHarness(messages, outreaches, contacts, **kw), messages, outreaches, contacts


class TestSendQueue:
    @pytest.mark.asyncio
    async def test_sends_approved_with_delays_and_updates_state(self):
        h, messages, outreaches, contacts = simple_setup(3)
        h.client.send_email = AsyncMock(side_effect=["conv-1", "conv-2", "conv-3"])
        report = await h.run()
        assert report.sent == 3
        assert h.client.send_email.await_count == 3
        assert h.sleep.await_count == 2
        for call in h.sleep.await_args_list:
            assert 60 <= call.args[0] <= 240
        assert all(m.status == MessageStatus.SENT for m in messages)
        assert outreaches[1].state == OutreachState.CONTACTED
        assert outreaches[1].follow_up_due == IN_WINDOW + timedelta(days=4)
        assert contacts[1].last_touch_at == IN_WINDOW
        assert h.settings_map["outreach_thread:1"] == "conv-1"
        assert h.settings_map["email_thread:conv-1"] == "1"

    @pytest.mark.asyncio
    async def test_dry_run_never_sends(self):
        h, messages, _, _ = simple_setup(2, dry_run=True)
        report = await h.run()
        assert report.dry_run == 2
        h.client.send_email.assert_not_called()
        h.sleep.assert_not_called()
        assert all(m.status == MessageStatus.APPROVED for m in messages)

    @pytest.mark.asyncio
    async def test_global_pause(self):
        h, _, _, _ = simple_setup(2, settings_map={"global_pause": "true"})
        report = await h.run()
        assert report.stopped == "paused"
        h.client.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_outside_window(self):
        late = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # 17:30 IST
        h, _, _, _ = simple_setup(2, now=late)
        report = await h.run()
        assert report.stopped == "outside_window"
        h.client.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_warmup_cap_stops_at_20(self):
        h, _, _, _ = simple_setup(5, sent_today=18)
        report = await h.run()
        assert report.sent == 2
        assert report.stopped == "daily_cap"

    @pytest.mark.asyncio
    async def test_full_cap_after_warmup(self):
        h, _, _, _ = simple_setup(5, sent_today=38, first_send_at=IN_WINDOW - timedelta(days=30))
        report = await h.run()
        assert report.sent == 2

    @pytest.mark.asyncio
    async def test_dnc_and_unsubscribed_rejected(self):
        messages = [make_message(1, 1), make_message(2, 2)]
        outreaches = {1: make_outreach(1), 2: make_outreach(2)}
        contacts = {1: make_contact(1, email="no@acme.com"), 2: make_contact(2, unsub=True)}
        h = QueueHarness(messages, outreaches, contacts, settings_map={"do_not_contact": "no@acme.com"})
        report = await h.run()
        assert report.blocked == 2
        assert all(m.status == MessageStatus.REJECTED for m in messages)
        h.client.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_company_unsubscribed_blocks(self):
        h, messages, _, _ = simple_setup(1, company_unsub=True)
        report = await h.run()
        assert report.blocked == 1
        h.client.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_fourteen_day_rule_skips_new_outreach(self):
        messages = [make_message(1, 1)]
        contacts = {1: make_contact(1, last_touch=IN_WINDOW - timedelta(days=3))}
        h = QueueHarness(messages, {1: make_outreach(1)}, contacts)
        report = await h.run()
        assert report.skipped == 1
        assert messages[0].status == MessageStatus.APPROVED
        h.client.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_follow_up_threads_and_bypasses_touch_rule(self):
        first_sent = IN_WINDOW - timedelta(days=4)
        outreach = make_outreach(1, state=OutreachState.CONTACTED)
        outreach.sent_at = first_sent
        contacts = {1: make_contact(1, last_touch=first_sent)}
        h = QueueHarness([make_message(1, 1)], {1: outreach}, contacts,
                         settings_map={"outreach_thread:1": "conv-9"})
        report = await h.run()
        assert report.sent == 1
        assert h.client.send_email.await_args.args[3] == "conv-9"
        assert outreach.follow_up_count == 1
        assert outreach.follow_up_due == first_sent + timedelta(days=10)

    @pytest.mark.asyncio
    async def test_send_failure_marks_failed(self):
        h, messages, outreaches, _ = simple_setup(1)
        h.client.send_email = AsyncMock(side_effect=GraphError(500, "boom"))
        report = await h.run()
        assert report.failed == 1
        assert messages[0].status == MessageStatus.FAILED
        assert outreaches[1].state == OutreachState.DRAFTED

    @pytest.mark.asyncio
    async def test_pause_during_delay_stops(self):
        h, _, _, _ = simple_setup(3)

        async def pause_on_sleep(_seconds):
            h.settings_map["global_pause"] = "1"

        h.sleep.side_effect = pause_on_sleep
        report = await h.run()
        assert report.sent == 1
        assert report.stopped == "paused"


# ─── Outlook / Graph ───────────────────────────────────────


class FakeTokens:
    def __init__(self):
        self.calls: list[bool] = []

    def get_token(self, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return "fresh" if force_refresh else "cached"


def graph_client(handler):
    tokens = FakeTokens()
    sleep = AsyncMock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OutlookClient(tokens, http=http, sleep=sleep), tokens, sleep


class TestOutlookHelpers:
    def test_unsubscribe_appended_once(self):
        body = with_unsubscribe("Hi")
        assert body.endswith(UNSUBSCRIBE_LINE)
        assert with_unsubscribe(body) == body

    def test_retry_after_parsing(self):
        assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "7"})) == 7
        assert retry_after_seconds(httpx.Response(429)) == 5.0
        assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "999"})) == 120


class TestOutlookClient:
    @pytest.mark.asyncio
    async def test_send_email_payload(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/sendMail"):
                return httpx.Response(202)
            return httpx.Response(200, json={"value": [{
                "conversationId": "conv-1", "subject": "Subj",
                "toRecipients": [{"emailAddress": {"address": "HR@acme.com"}}],
            }]})

        client, _, _ = graph_client(handler)
        conv = await client.send_email("hr@acme.com", "Subj", "Body")
        payload = json.loads(seen[0].content)
        assert seen[0].method == "POST"
        assert payload["saveToSentItems"] is True
        assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "hr@acme.com"
        assert UNSUBSCRIBE_LINE in payload["message"]["body"]["content"]
        assert seen[0].headers["Authorization"] == "Bearer cached"
        assert conv == "conv-1"

    @pytest.mark.asyncio
    async def test_send_with_conversation_replies_in_thread(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            path = request.url.path
            if path == "/v1.0/me/messages" and request.method == "GET":
                return httpx.Response(200, json={"value": [
                    {"id": "old", "receivedDateTime": "2026-10-01T00:00:00Z"},
                    {"id": "new", "receivedDateTime": "2026-10-02T00:00:00Z"},
                ]})
            if path.endswith("/createReply"):
                return httpx.Response(201, json={"id": "draft-1", "conversationId": "conv-9"})
            return httpx.Response(202)

        client, _, _ = graph_client(handler)
        conv = await client.send_email("hr@acme.com", "S", "Follow up", conversation_id="conv-9")
        assert conv == "conv-9"
        assert "conversationId eq 'conv-9'" in seen[0].url.params["$filter"]
        assert seen[1].url.path == "/v1.0/me/messages/new/createReply"
        assert UNSUBSCRIBE_LINE in json.loads(seen[1].content)["comment"]
        assert seen[2].url.path == "/v1.0/me/messages/draft-1/send"
        assert not any(r.url.path.endswith("/sendMail") for r in seen)

    @pytest.mark.asyncio
    async def test_429_honours_retry_after(self):
        responses = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(202)]
        client, _, sleep = graph_client(lambda request: responses.pop(0))
        await client._request("POST", "/me/sendMail", json={})
        sleep.assert_awaited_once_with(3.0)

    @pytest.mark.asyncio
    async def test_429_gives_up_after_retries(self):
        client, _, sleep = graph_client(lambda request: httpx.Response(429))
        with pytest.raises(GraphError) as exc:
            await client._request("GET", "/me/messages")
        assert exc.value.status_code == 429
        assert sleep.await_count == outlook.MAX_RETRIES

    @pytest.mark.asyncio
    async def test_401_forces_token_refresh_once(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["Authorization"] == "Bearer cached":
                return httpx.Response(401)
            return httpx.Response(200, json={})

        client, tokens, _ = graph_client(handler)
        response = await client._request("GET", "/me/messages")
        assert response.status_code == 200
        assert tokens.calls == [False, True]

    @pytest.mark.asyncio
    async def test_401_twice_raises(self):
        client, _, _ = graph_client(lambda request: httpx.Response(401))
        with pytest.raises(GraphError):
            await client._request("GET", "/me/messages")

    @pytest.mark.asyncio
    async def test_list_new_replies_filters_and_pages(self):
        seen = []
        pages = [
            {"value": [
                {"id": "a", "conversationId": "conv-1", "from": {"emailAddress": {"address": "hr@acme.com"}}},
                {"id": "b", "conversationId": "spam"},
            ], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skip=50"},
            {"value": [{"id": "c", "conversationId": "conv-2"}]},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=pages.pop(0))

        client, _, _ = graph_client(handler)
        since = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)
        replies = await client.list_new_replies(since, {"conv-1", "conv-2"})
        assert [r["id"] for r in replies] == ["a", "c"]
        assert replies[0]["from"] == "hr@acme.com"
        assert seen[0].url.path == "/v1.0/me/mailFolders/inbox/messages"
        assert seen[0].url.params["$filter"] == "receivedDateTime ge 2026-10-01T04:30:00Z"
        assert seen[1].url.params["$skip"] == "50"


class FakeCache:
    def __init__(self):
        self.has_state_changed = False
        self.loaded = None

    def deserialize(self, data: str) -> None:
        self.loaded = data

    def serialize(self) -> str:
        return "serialized"


class TestTokenProvider:
    def make(self, tmp_path, app):
        cache = FakeCache()
        provider = TokenProvider(
            "client", "common", str(tmp_path / "creds" / "cache.json"),
            app_factory=lambda cid, authority, c: app, cache_factory=lambda: cache,
        )
        return provider, cache

    def test_silent_refresh_used_when_account_cached(self, tmp_path):
        app = MagicMock()
        app.get_accounts.return_value = [{"username": "me"}]
        app.acquire_token_silent.return_value = {"access_token": "tok"}
        provider, _ = self.make(tmp_path, app)
        assert provider.get_token(force_refresh=True) == "tok"
        assert app.acquire_token_silent.call_args.kwargs["force_refresh"] is True
        app.initiate_device_flow.assert_not_called()

    def test_device_flow_when_no_account_and_cache_saved(self, tmp_path):
        app = MagicMock()
        app.get_accounts.return_value = []
        app.initiate_device_flow.return_value = {"user_code": "ABC", "message": "go"}
        app.acquire_token_by_device_flow.return_value = {"access_token": "dev"}
        provider, cache = self.make(tmp_path, app)
        cache.has_state_changed = True
        assert provider.get_token() == "dev"
        assert (tmp_path / "creds" / "cache.json").read_text() == "serialized"

    def test_existing_cache_file_loaded(self, tmp_path):
        path = tmp_path / "creds" / "cache.json"
        path.parent.mkdir()
        path.write_text("stored")
        _, cache = self.make(tmp_path, MagicMock())
        assert cache.loaded == "stored"


# ─── Channel interface ─────────────────────────────────────


class TestChannelInterface:
    def test_outlook_client_implements_channel(self):
        from app.channels.base import Channel

        client = OutlookClient(FakeTokens(), http=httpx.AsyncClient())
        assert isinstance(client, Channel)

    @pytest.mark.asyncio
    async def test_send_message_wraps_send_email(self):
        from app.channels.base import Thread

        client, _, _ = graph_client(lambda request: httpx.Response(202))
        client.send_email = AsyncMock(return_value="conv-1")
        out = await client.send_message(Thread("a@b.com", "Subj", "conv-1"), "Hi")
        client.send_email.assert_awaited_once_with("a@b.com", "Subj", "Hi", "conv-1")
        assert out == "conv-1"

    @pytest.mark.asyncio
    async def test_read_new_replies_returns_incoming(self):
        payload = {"value": [{
            "id": "m1", "conversationId": "c1", "bodyPreview": "Yes",
            "from": {"emailAddress": {"address": "hr@acme.com"}},
            "receivedDateTime": "2026-10-02T00:00:00Z",
        }]}
        client, _, _ = graph_client(lambda request: httpx.Response(200, json=payload))
        replies = await client.read_new_replies(IN_WINDOW)
        assert replies[0].thread_id == "c1"
        assert replies[0].sender == "hr@acme.com"
        assert replies[0].text == "Yes"

    @pytest.mark.asyncio
    async def test_reply_threads_or_falls_back(self):
        from app.channels.base import Thread

        client, _, _ = graph_client(lambda request: httpx.Response(200))
        client._latest_in_conversation = AsyncMock(return_value="msg-1")
        client.reply_in_thread = AsyncMock(return_value="c1")
        client.send_message = AsyncMock(return_value="c2")
        assert await client.reply(Thread("a@b.com", "S", "c1"), "Hi") == "c1"
        client._latest_in_conversation.return_value = None
        assert await client.reply(Thread("a@b.com", "S", "c1"), "Hi") == "c2"
        assert await client.reply(Thread("a@b.com", "S"), "Hi") == "c2"
