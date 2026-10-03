from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import attention
from app.auth.deps import get_current_user
from app.channels.base import IncomingReply
from app.config import settings
from app.db import get_session
from app.db.models import (
    AttentionItem,
    AttentionStatus,
    AttentionType,
    ChannelType,
    Company,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    ReviewStatus,
)
from app.llm.prompts import ReplyClassificationResult
from app.workers.followups import FollowUpWorker
from app.workers.replies import AnswerDraft, ReplyWorker, classify_reply
from app.workers.review import ReviewCommands
from tests.test_send_queue import QueueHarness, make_contact, make_message, make_outreach

IN_WINDOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
LATE = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # 17:30 IST
PROFILE = "https://linkedin.com/in/jane"


@pytest.fixture(autouse=True)
def tpo(monkeypatch):
    monkeypatch.setattr(settings, "tpo_name", "Dr. Rao")
    monkeypatch.setattr(settings, "tpo_phone", "+91 90000 00000")
    monkeypatch.setattr(settings, "tpo_email", "tpo@college.edu")


def incoming(text="Sounds good", msg_id="r1", thread="t1", sender=PROFILE):
    return IncomingReply(message_id=msg_id, thread_id=thread, sender=sender, text=text,
                         received_at="2026-10-05T05:00:00Z")


def classification(intent, **kw):
    return ReplyClassificationResult(intent=intent, **kw)


class FakeLLM:
    def __init__(self, result, answer="We offer 6-month internships."):
        self.result = result
        self.answer = answer
        self.prompts: list[str] = []

    async def complete_json(self, prompt, schema):
        self.prompts.append(prompt)
        if schema is AnswerDraft:
            if isinstance(self.answer, Exception):
                raise self.answer
            return AnswerDraft(message=self.answer)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ReplyHarness:
    def __init__(self, result, channel=ChannelType.LINKEDIN, state=OutreachState.DM_SENT,
                 email="jane@acme.com", **kw):
        self.outreach = SimpleNamespace(
            id=1, contact_id=2, post_id=9, channel=channel, state=state,
            follow_up_due=IN_WINDOW + timedelta(days=3), follow_up_count=0,
        )
        self.contact = SimpleNamespace(
            id=2, company_id=3, name="Jane Doe", email=email, linkedin_url=PROFILE,
            is_unsubscribed=kw.get("unsub", False),
        )
        objects = {(Outreach, 1): self.outreach, (Contact, 2): self.contact,
                   (Company, 3): SimpleNamespace(id=3, name="Acme")}
        self.added: list = []
        self.db = MagicMock()
        self.db.get.side_effect = lambda model, key: objects.get((model, key))
        self.db.add.side_effect = self.added.append
        self.db.flush.side_effect = self._assign_ids
        self.notify = AsyncMock(return_value="wamid")
        self.llm = FakeLLM(result, kw.get("answer", "We offer 6-month internships."))
        self.linkedin = MagicMock()
        self.linkedin.read_new_replies = AsyncMock(return_value=kw.get("replies", []))
        self.outlook = MagicMock()
        self.outlook.read_new_replies = AsyncMock(return_value=kw.get("email_replies", []))
        self.settings_map = {f"{channel.value}_thread:t1": "1", **kw.get("settings_map", {})}
        self.pending = kw.get("pending", [])
        self.worker = ReplyWorker(
            db=self.db,
            channels={ChannelType.LINKEDIN: self.linkedin, ChannelType.EMAIL: self.outlook},
            llm=self.llm, notify=self.notify, clock=lambda: kw.get("now", IN_WINDOW),
        )
        w = self.worker
        w._setting = self.settings_map.get
        w._set_setting = self.settings_map.__setitem__
        w._outreach_for_sender = lambda channel, sender: None
        w._pending_outbound = lambda outreach_id: self.pending
        w._email_outreach = lambda contact_id: kw.get("email_outreach")
        w._last_sent_text = lambda outreach_id: "Hi Jane, AI ConnectX here"

    def _assign_ids(self):
        for index, obj in enumerate(self.added, start=100):
            if isinstance(obj, Message) and obj.id is None:
                obj.id = index

    def of(self, cls, **match):
        return [o for o in self.added if isinstance(o, cls)
                and all(getattr(o, k) == v for k, v in match.items())]

    def outbound(self):
        return self.of(Message, direction=MessageDirection.OUTBOUND)


# ─── Polling ───────────────────────────────────────────────


class TestPolling:
    @pytest.mark.asyncio
    async def test_outside_window_does_not_poll(self):
        h = ReplyHarness(classification("other"), now=LATE)
        report = await h.worker.run_once()
        assert report.stopped == "outside_window"
        h.linkedin.read_new_replies.assert_not_called()

    @pytest.mark.asyncio
    async def test_linkedin_stub_is_skipped(self):
        h = ReplyHarness(classification("not_now"))
        h.linkedin.read_new_replies = AsyncMock(side_effect=NotImplementedError)
        report = await h.worker.run_once()
        assert report.errors == 0
        h.outlook.read_new_replies.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cursor_dedupe_and_first_lookback(self):
        h = ReplyHarness(classification("not_now"), replies=[incoming(msg_id="r1")],
                         settings_map={"replies_seen:linkedin": json.dumps(["r1"])})
        report = await h.worker.run_once()
        assert report.handled == 0
        since = h.linkedin.read_new_replies.await_args.args[0]
        assert since == IN_WINDOW - timedelta(days=1)
        assert h.settings_map["replies_cursor:linkedin"] == IN_WINDOW.isoformat()
        assert json.loads(h.settings_map["replies_seen:linkedin"]) == ["r1"]

    @pytest.mark.asyncio
    async def test_uses_saved_cursor(self):
        cursor = IN_WINDOW - timedelta(minutes=30)
        h = ReplyHarness(classification("not_now"),
                         settings_map={"replies_cursor:email": cursor.isoformat()})
        await h.worker.run_once()
        assert h.outlook.read_new_replies.await_args.args[0] == cursor

    @pytest.mark.asyncio
    async def test_unknown_thread_ignored(self):
        h = ReplyHarness(classification("interested"), replies=[incoming(thread="other")])
        report = await h.worker.run_once()
        assert report.ignored == 1
        assert h.added == []

    @pytest.mark.asyncio
    async def test_channel_error_creates_attention(self):
        h = ReplyHarness(classification("other"))
        h.outlook.read_new_replies = AsyncMock(side_effect=RuntimeError("graph down"))
        report = await h.worker.run_once()
        assert report.errors == 1
        assert h.of(AttentionItem, type=AttentionType.CHANNEL_ERROR)

    @pytest.mark.asyncio
    async def test_classification_failure_falls_back_to_other(self):
        result = await classify_reply("hm", FakeLLM(RuntimeError("all providers down")))
        assert result.intent == "other"


# ─── State machine ─────────────────────────────────────────


class TestIntents:
    @pytest.mark.asyncio
    async def test_reply_stores_inbound_and_cancels_pending(self):
        pending = SimpleNamespace(status=MessageStatus.APPROVED, review_status=ReviewStatus.PENDING_REVIEW)
        h = ReplyHarness(classification("not_now"), pending=[pending])
        await h.worker.handle(ChannelType.LINKEDIN, incoming("Maybe next quarter"))
        inbound = h.of(Message, direction=MessageDirection.INBOUND)
        assert inbound[0].content == "Maybe next quarter"
        assert pending.status == MessageStatus.REJECTED
        assert pending.review_status == ReviewStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_interested_linkedin_with_email(self):
        h = ReplyHarness(classification("interested"))
        intent = await h.worker.handle(ChannelType.LINKEDIN, incoming("Yes, let's talk"))
        assert intent == "interested"
        assert h.outreach.state == OutreachState.INTERESTED
        tpo_msg, mou_msg = h.outbound()
        assert tpo_msg.outreach_id == 1
        assert "Dr. Rao" in tpo_msg.content and "+91 90000 00000" in tpo_msg.content
        assert "tpo@college.edu" in tpo_msg.content
        assert tpo_msg.status == MessageStatus.APPROVED and tpo_msg.hold_until is None
        email_outreach = mou_msg.outreach
        assert email_outreach.channel == ChannelType.EMAIL
        assert email_outreach.state == OutreachState.INTERESTED
        assert "Memorandum of Understanding" in mou_msg.content
        item = h.of(AttentionItem, type=AttentionType.INTERESTED)[0]
        assert "Yes, let's talk" in item.payload["summary"]
        alert = h.notify.await_args.args[0]
        assert alert.startswith("Interested: Jane Doe (Acme) on LinkedIn")
        assert "MoU email" in alert

    @pytest.mark.asyncio
    async def test_interested_reuses_existing_email_outreach(self):
        existing = SimpleNamespace(id=5, channel=ChannelType.EMAIL, state=OutreachState.CONTACTED,
                                   follow_up_due=IN_WINDOW)
        h = ReplyHarness(classification("interested"), email_outreach=existing)
        await h.worker.handle(ChannelType.LINKEDIN, incoming())
        assert h.outbound()[1].outreach_id == 5
        assert existing.state == OutreachState.INTERESTED
        assert existing.follow_up_due is None

    @pytest.mark.asyncio
    async def test_interested_without_email_only_dms(self):
        h = ReplyHarness(classification("interested"), email=None)
        await h.worker.handle(ChannelType.LINKEDIN, incoming())
        assert len(h.outbound()) == 1
        assert "MoU email" not in h.notify.await_args.args[0]

    @pytest.mark.asyncio
    async def test_interested_email_thread_stays_on_email(self):
        h = ReplyHarness(classification("interested"), channel=ChannelType.EMAIL,
                         state=OutreachState.CONTACTED)
        await h.worker.handle(ChannelType.EMAIL, incoming())
        assert [m.outreach_id for m in h.outbound()] == [1, 1]

    @pytest.mark.asyncio
    async def test_email_from_reply_saved_on_contact(self):
        h = ReplyHarness(classification("interested", email="Jane@Acme.com"), email=None)
        await h.worker.handle(ChannelType.LINKEDIN, incoming())
        assert h.contact.email == "jane@acme.com"
        assert len(h.outbound()) == 2

    @pytest.mark.asyncio
    async def test_no_opportunity_future_pool(self):
        h = ReplyHarness(classification("no_opportunity"))
        await h.worker.handle(ChannelType.LINKEDIN, incoming("No openings now"))
        assert h.outreach.state == OutreachState.FUTURE_POOL
        assert h.outreach.follow_up_due == IN_WINDOW + timedelta(days=60)
        text = h.outbound()[0].content
        assert "MoU" in text and "phone number and email" in text

    @pytest.mark.asyncio
    async def test_wrong_person_retargets_referral(self):
        h = ReplyHarness(classification("wrong_person", referral_name="Ravi Kumar"))
        await h.worker.handle(ChannelType.LINKEDIN, incoming("Talk to Ravi Kumar"))
        assert h.outreach.state == OutreachState.WRONG_PERSON
        assert "Ravi Kumar" in h.outbound()[0].content
        new_contact = h.of(Contact)[0]
        assert new_contact.name == "Ravi Kumar" and new_contact.company_id == 3
        new_outreach = h.of(Outreach)[0]
        assert new_outreach.contact is new_contact
        assert new_outreach.state == OutreachState.TARGETED
        assert new_outreach.channel == ChannelType.LINKEDIN
        assert h.of(AttentionItem, type=AttentionType.NEEDS_MANUAL)

    @pytest.mark.asyncio
    async def test_wrong_person_without_name_asks_for_referral(self):
        h = ReplyHarness(classification("wrong_person"))
        await h.worker.handle(ChannelType.LINKEDIN, incoming("Not me"))
        assert "point me to" in h.outbound()[0].content
        assert h.of(Contact) == []

    @pytest.mark.asyncio
    async def test_not_now_follow_up_in_30_days(self):
        h = ReplyHarness(classification("not_now"))
        await h.worker.handle(ChannelType.LINKEDIN, incoming())
        assert h.outreach.state == OutreachState.NOT_NOW
        assert h.outreach.follow_up_due == IN_WINDOW + timedelta(days=30)
        assert h.outbound()[0].status == MessageStatus.APPROVED

    @pytest.mark.asyncio
    async def test_question_drafts_answer_waits_for_approval(self):
        h = ReplyHarness(classification("question"))
        await h.worker.handle(ChannelType.LINKEDIN, incoming("How long are internships?"))
        draft = h.outbound()[0]
        assert draft.status == MessageStatus.DRAFTED
        assert draft.content == "We offer 6-month internships."
        item = h.of(AttentionItem, type=AttentionType.QUESTION)[0]
        assert item.payload["message_id"] == draft.id
        assert item.payload["draft"] == draft.content
        alert = h.notify.await_args.args[0]
        assert "How long are internships?" in alert
        assert f"SEND {draft.id} | CANCEL {draft.id} | EDIT {draft.id}" in alert
        assert "Waits for your SEND." in alert
        assert "How long are internships?" in h.llm.prompts[-1]

    @pytest.mark.asyncio
    async def test_question_draft_failure_goes_to_attention(self):
        h = ReplyHarness(classification("question"), answer=RuntimeError("llm down"))
        await h.worker.handle(ChannelType.LINKEDIN, incoming("?"))
        assert h.outbound() == []
        assert h.of(AttentionItem, type=AttentionType.NEEDS_MANUAL)

    @pytest.mark.asyncio
    async def test_unsubscribe_dnc_forever_no_reply(self):
        h = ReplyHarness(classification("unsubscribe"), settings_map={"do_not_contact": "x@y.com"})
        await h.worker.handle(ChannelType.LINKEDIN, incoming("Stop messaging me"))
        assert h.outreach.state == OutreachState.UNSUBSCRIBED
        assert h.contact.is_unsubscribed is True
        dnc = h.settings_map["do_not_contact"].split("\n")
        assert {"x@y.com", "jane@acme.com", PROFILE} <= set(dnc)
        assert h.outbound() == []
        h.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsubscribed_contact_gets_nothing(self):
        h = ReplyHarness(classification("interested"), unsub=True)
        await h.worker.handle(ChannelType.LINKEDIN, incoming())
        assert h.outbound() == []
        assert h.llm.prompts == []

    @pytest.mark.asyncio
    async def test_other_needs_manual(self):
        h = ReplyHarness(classification("other"))
        await h.worker.handle(ChannelType.LINKEDIN, incoming("??"))
        assert h.outreach.state == OutreachState.REPLIED
        assert h.of(AttentionItem, type=AttentionType.NEEDS_MANUAL)
        assert h.notify.await_count == 1


class TestQuestionApproval:
    def _db(self, message, outreach):
        objects = {(Message, 7): message, (Outreach, 1): outreach,
                   (Contact, 2): SimpleNamespace(id=2, company_id=3, name="Jane"),
                   (Company, 3): SimpleNamespace(id=3, name="Acme")}
        db = MagicMock()
        db.get.side_effect = lambda model, key: objects.get((model, key))
        db.scalars.return_value = iter([])
        return db

    def _draft(self):
        return SimpleNamespace(
            id=7, outreach_id=1, content="Answer", edited_text=None,
            direction=MessageDirection.OUTBOUND, status=MessageStatus.DRAFTED,
            review_status=ReviewStatus.PENDING_REVIEW, hold_until=None, reviewed_at=None,
        )

    @pytest.mark.asyncio
    async def test_send_approves_reply_draft(self):
        draft = self._draft()
        outreach = SimpleNamespace(id=1, contact_id=2, state=OutreachState.REPLIED,
                                   channel=ChannelType.LINKEDIN)
        db = self._db(draft, outreach)
        cmd = ReviewCommands(session_factory=lambda: db, notify=AsyncMock(), clock=lambda: IN_WINDOW)
        assert "released" in await cmd.send(7)
        assert draft.status == MessageStatus.APPROVED
        assert draft.hold_until == IN_WINDOW

    @pytest.mark.asyncio
    async def test_edit_reply_draft_keeps_waiting(self):
        draft = self._draft()
        outreach = SimpleNamespace(id=1, contact_id=2, state=OutreachState.REPLIED,
                                   channel=ChannelType.LINKEDIN)
        notify = AsyncMock()
        db = self._db(draft, outreach)
        cmd = ReviewCommands(session_factory=lambda: db, notify=notify, clock=lambda: IN_WINDOW)
        assert "SEND 7" in await cmd.edit(7, "Better answer")
        assert draft.status == MessageStatus.DRAFTED
        assert draft.hold_until is None
        assert '"Better answer"\nWaits for your SEND.' in notify.await_args.args[0]


# ─── Send queue: auto-replies ──────────────────────────────


def reply_harness(channel=ChannelType.LINKEDIN, **kw):
    outreach = make_outreach(1, state=OutreachState.NOT_NOW, channel=channel)
    contact = make_contact(1, linkedin=PROFILE if channel == ChannelType.LINKEDIN else None,
                           last_touch=IN_WINDOW - timedelta(days=1))
    h = QueueHarness([make_message(1, 1)], {1: outreach}, {1: contact},
                     settings_map={"outreach_thread:1": "t1"}, **kw)
    h.client.reply = AsyncMock(return_value="conv-1")
    h.linkedin.reply = AsyncMock(return_value="t1")
    return h, outreach


class TestQueueReplies:
    @pytest.mark.asyncio
    async def test_linkedin_reply_waits_3_to_12_min_in_thread(self):
        h, outreach = reply_harness()
        report = await h.run()
        assert report.sent == 1
        assert 180 <= h.sleep.await_args.args[0] <= 720
        thread, text = h.linkedin.reply.await_args.args
        assert thread.recipient == PROFILE and thread.thread_id == "t1"
        assert outreach.state == OutreachState.NOT_NOW
        h.linkedin.send_dm.assert_not_called()

    @pytest.mark.asyncio
    async def test_email_reply_in_thread_without_linkedin_delay(self):
        h, _ = reply_harness(ChannelType.EMAIL)
        report = await h.run()
        assert report.sent == 1
        h.sleep.assert_not_called()
        assert h.client.reply.await_args.args[0].thread_id == "t1"

    @pytest.mark.asyncio
    async def test_linkedin_reply_counts_toward_cap(self):
        h, _ = reply_harness(sent_today={ChannelType.LINKEDIN: 20})
        report = await h.run()
        assert report.stopped == "daily_cap"
        h.linkedin.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_linkedin_reply_respects_dry_run(self):
        h, _ = reply_harness(dry_run=True)
        report = await h.run()
        assert report.dry_run == 1
        h.sleep.assert_not_called()
        h.linkedin.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_linkedin_reply_respects_pause_and_window(self):
        h, _ = reply_harness()
        h.settings_map["global_pause"] = "true"
        assert (await h.run()).stopped == "paused"
        late, _ = reply_harness(now=LATE)
        assert (await late.run()).stopped == "outside_window"
        h.linkedin.reply.assert_not_called()
        late.linkedin.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_during_linkedin_delay_stops(self):
        h, _ = reply_harness()

        async def pause(_seconds):
            h.settings_map["global_pause"] = "1"

        h.sleep.side_effect = pause
        report = await h.run()
        assert report.stopped == "paused"
        h.linkedin.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_linkedin_skips_without_waiting(self):
        h, _ = reply_harness()
        h.linkedin.available = False
        report = await h.run()
        assert report.skipped == 1
        h.sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_reply_goes_through_hold(self):
        outreach = make_outreach(1, state=OutreachState.INTERESTED, channel=ChannelType.LINKEDIN)
        h = QueueHarness([make_message(1, 1, held=False)], {1: outreach},
                         {1: make_contact(1, linkedin=PROFILE)})
        report = await h.run()
        assert report.held == 1
        h.review.start_hold.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mou_email_new_thread_subject(self):
        outreach = make_outreach(1, state=OutreachState.INTERESTED)
        h = QueueHarness([make_message(1, 1)], {1: outreach}, {1: make_contact(1)})
        h.client.reply = AsyncMock(return_value="conv-7")
        await h.run()
        thread = h.client.reply.await_args.args[0]
        assert thread.subject == "MoU proposal: AI ConnectX x Acme"
        assert thread.thread_id is None
        assert h.settings_map["email_thread:conv-7"] == "1"

    @pytest.mark.asyncio
    async def test_linkedin_thread_saved_for_reply_matching(self):
        h, _ = reply_harness()
        h.settings_map.pop("outreach_thread:1")
        h.linkedin.reply = AsyncMock(return_value="li-9")
        await h.run()
        assert h.settings_map["linkedin_thread:li-9"] == "1"


# ─── Follow-ups ────────────────────────────────────────────


class FollowHarness:
    def __init__(self, outreaches, pending=False, unsub=False):
        self.added: list = []
        contact = SimpleNamespace(id=2, company_id=3, name="Jane Doe", is_unsubscribed=unsub)
        objects = {(Contact, 2): contact, (Company, 3): SimpleNamespace(id=3, name="Acme")}
        self.db = MagicMock()
        self.db.get.side_effect = lambda model, key: objects.get((model, key))
        self.db.add.side_effect = self.added.append
        self.worker = FollowUpWorker(db=self.db, clock=lambda: IN_WINDOW)
        self.worker._due_outreaches = lambda now: outreaches
        self.worker._has_pending = lambda outreach_id: pending


def due_outreach(state, count=0, channel=ChannelType.EMAIL):
    return SimpleNamespace(id=1, contact_id=2, state=state, channel=channel,
                           follow_up_count=count, follow_up_due=IN_WINDOW - timedelta(hours=1))


class TestFollowUps:
    def test_day_4_follow_up_queued_same_outreach(self):
        outreach = due_outreach(OutreachState.CONTACTED)
        h = FollowHarness([outreach])
        assert h.worker.run_once().queued == 1
        message = h.added[0]
        assert message.outreach_id == 1
        assert message.status == MessageStatus.APPROVED
        assert message.hold_until is None
        assert "following up" in message.content and "Acme" in message.content

    def test_day_10_uses_second_text(self):
        h = FollowHarness([due_outreach(OutreachState.DM_SENT, count=1, channel=ChannelType.LINKEDIN)])
        h.worker.run_once()
        assert "one last note" in h.added[0].content

    def test_closed_after_last_follow_up(self):
        outreach = due_outreach(OutreachState.CONTACTED, count=2)
        h = FollowHarness([outreach])
        assert h.worker.run_once().closed == 1
        assert outreach.state == OutreachState.CLOSED
        assert h.added == []

    def test_pending_message_not_duplicated(self):
        h = FollowHarness([due_outreach(OutreachState.CONTACTED)], pending=True)
        assert h.worker.run_once().skipped == 1
        assert h.added == []

    @pytest.mark.parametrize("state", [OutreachState.NOT_NOW, OutreachState.FUTURE_POOL])
    def test_recheck_once(self, state):
        outreach = due_outreach(state)
        h = FollowHarness([outreach])
        h.worker.run_once()
        assert "checking" in h.added[0].content.lower()
        assert outreach.follow_up_due is None

    def test_unsubscribed_skipped(self):
        h = FollowHarness([due_outreach(OutreachState.CONTACTED)], unsub=True)
        assert h.worker.run_once().skipped == 1

    @pytest.mark.asyncio
    async def test_queue_sets_close_date_after_last_follow_up(self):
        outreach = make_outreach(1, state=OutreachState.CONTACTED)
        outreach.sent_at = IN_WINDOW - timedelta(days=10)
        outreach.follow_up_count = 1
        contact = make_contact(1, last_touch=IN_WINDOW - timedelta(days=6))
        h = QueueHarness([make_message(1, 1)], {1: outreach}, {1: contact})
        await h.run()
        assert outreach.follow_up_count == 2
        assert outreach.follow_up_due == IN_WINDOW + timedelta(days=4)


# ─── Attention API ─────────────────────────────────────────


class AttentionDB:
    def __init__(self, items, messages=None):
        self.items = {item.id: item for item in items}
        self.messages = messages or {}
        self.commits = 0

    def get(self, model, key):
        return (self.items if model is AttentionItem else self.messages).get(key)

    def scalars(self, stmt):
        if "attention_items" in str(stmt) and "messages" not in str(stmt):
            return iter(self.items.values())
        return iter([])

    def commit(self):
        self.commits += 1


def item(item_id=1, kind=AttentionType.QUESTION, payload=None, state=AttentionStatus.OPEN):
    return AttentionItem(id=item_id, type=kind, payload=payload or {}, status=state,
                         created_at=IN_WINDOW)


def draft_message():
    return SimpleNamespace(id=7, status=MessageStatus.DRAFTED, review_status=ReviewStatus.PENDING_REVIEW,
                           hold_until=None, reviewed_at=None, edited_text=None)


def api(db):
    app = FastAPI()
    app.include_router(attention.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=7, role="member")
    return TestClient(app)


class TestAttentionAPI:
    def test_list(self):
        db = AttentionDB([item(payload={"draft": "x"})])
        rows = api(db).get("/attention").json()
        assert rows[0]["type"] == "question" and rows[0]["payload"] == {"draft": "x"}

    def test_approve_releases_draft(self):
        message = draft_message()
        db = AttentionDB([item(payload={"message_id": 7})], {7: message})
        r = api(db).post("/attention/1/approve")
        assert r.status_code == 200 and r.json()["status"] == "resolved"
        assert message.status == MessageStatus.APPROVED
        assert message.review_status == ReviewStatus.APPROVED
        assert message.hold_until is not None

    def test_approve_without_message_just_resolves(self):
        db = AttentionDB([item(kind=AttentionType.INTERESTED)])
        assert api(db).post("/attention/1/approve").json()["status"] == "resolved"

    def test_approve_sent_message_conflict(self):
        message = draft_message()
        message.status = MessageStatus.SENT
        db = AttentionDB([item(payload={"message_id": 7})], {7: message})
        assert api(db).post("/attention/1/approve").status_code == 409

    def test_dismiss_cancels_draft(self):
        message = draft_message()
        db = AttentionDB([item(payload={"message_id": 7})], {7: message})
        r = api(db).post("/attention/1/dismiss")
        assert r.json()["status"] == "dismissed"
        assert message.review_status == ReviewStatus.CANCELLED
        assert message.status == MessageStatus.REJECTED

    def test_closed_item_conflict(self):
        db = AttentionDB([item(state=AttentionStatus.RESOLVED)])
        assert api(db).post("/attention/1/dismiss").status_code == 409

    def test_patch_edits_draft(self):
        message = draft_message()
        db = AttentionDB([item(payload={"message_id": 7, "draft": "old"})], {7: message})
        r = api(db).patch("/attention/1", json={"draft": "new answer"})
        assert r.json()["payload"]["draft"] == "new answer"
        assert message.edited_text == "new answer"
        assert message.review_status == ReviewStatus.EDITED
        assert message.status == MessageStatus.DRAFTED

    def test_patch_status(self):
        db = AttentionDB([item()])
        assert api(db).patch("/attention/1", json={"status": "resolved"}).json()["status"] == "resolved"

    def test_missing_item_404(self):
        assert api(AttentionDB([])).post("/attention/9/approve").status_code == 404
