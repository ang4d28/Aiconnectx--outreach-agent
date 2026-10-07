from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import reports as reports_api
from app.db import get_session
from app.db.models import ChannelType, NotificationLog
from app.workers import reports
from app.workers.reports import (
    DAILY_SUMMARY,
    PENDING,
    SEND_BATCH,
    SENT_NOTICE,
    SendReporter,
    Stats,
    collect_stats,
    send_daily_summary_if_due,
    summary_due,
)

IN_WINDOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
EVENING = datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)  # 20:00 IST


class ReporterHarness:
    def __init__(self, notify=None):
        self.rows: list[NotificationLog] = []
        self.db = MagicMock()
        self.db.add.side_effect = self.rows.append
        self.notify = notify or AsyncMock(return_value="wamid")
        self.now = IN_WINDOW
        self.reporter = SendReporter(self.db, notify=self.notify, clock=lambda: self.now)
        self.reporter._pending = lambda: [r for r in self.rows if r.type == PENDING]
        self.reporter._recent_sends = lambda now: len(
            [r for r in self.rows if r.type in (PENDING, SENT_NOTICE)
             and r.sent_at >= now - timedelta(minutes=10)]
        )

    async def send(self, n: int, channel=ChannelType.EMAIL):
        await self.reporter.record_send(n, channel, f"Person {n}", "Acme")

    def types(self, kind):
        return [r for r in self.rows if r.type == kind]


class TestSendNotices:
    @pytest.mark.asyncio
    async def test_single_send_notifies_one_line_and_logs(self):
        h = ReporterHarness()
        await h.send(7, ChannelType.LINKEDIN)
        h.notify.assert_awaited_once_with("Sent #7 via LinkedIn to Person 7 (Acme)")
        assert len(h.types(SENT_NOTICE)) == 1 and not h.types(PENDING)

    @pytest.mark.asyncio
    async def test_first_five_notify_individually(self):
        h = ReporterHarness()
        for n in range(1, 6):
            await h.send(n)
        assert h.notify.await_count == 5
        assert h.notify.await_args.args[0] == "Sent #5 via Email to Person 5 (Acme)"

    @pytest.mark.asyncio
    async def test_burst_over_five_is_held_then_batched(self):
        h = ReporterHarness()
        for n in range(1, 9):
            await h.send(n)
        assert h.notify.await_count == 5  # sends 6-8 held back
        assert len(h.types(PENDING)) == 3
        assert await h.reporter.flush() == 3
        assert h.notify.await_count == 6
        batch = h.notify.await_args.args[0]
        assert batch.startswith("Sent 3 messages:\n")
        assert "#6 via Email to Person 6 (Acme)" in batch and "#8 via" in batch
        assert not h.types(PENDING)
        assert h.types(SEND_BATCH)[0].payload["message_ids"] == [6, 7, 8]

    @pytest.mark.asyncio
    async def test_sends_outside_ten_minute_window_not_batched(self):
        h = ReporterHarness()
        for n in range(1, 6):
            await h.send(n)
        h.now = IN_WINDOW + timedelta(minutes=11)
        await h.send(6)
        assert h.notify.await_count == 6
        assert h.notify.await_args.args[0].startswith("Sent #6")

    @pytest.mark.asyncio
    async def test_flush_single_pending_is_one_line(self):
        h = ReporterHarness()
        for n in range(1, 7):
            await h.send(n)
        assert await h.reporter.flush() == 1
        assert h.notify.await_args.args[0] == "Sent #6 via Email to Person 6 (Acme)"
        assert not h.types(SEND_BATCH)

    @pytest.mark.asyncio
    async def test_flush_with_nothing_pending(self):
        h = ReporterHarness()
        assert await h.reporter.flush() == 0
        h.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_whatsapp_failure_keeps_notice_pending_and_never_raises(self):
        h = ReporterHarness(notify=AsyncMock(side_effect=RuntimeError("down")))
        await h.send(1)
        assert len(h.types(PENDING)) == 1
        h.notify.side_effect = None
        assert await h.reporter.flush() == 1
        assert not h.types(PENDING)


# ─── Daily summary ─────────────────────────────────────────


def make_stats():
    return Stats(
        date="Mon 05 Oct",
        sent={"linkedin": 9, "email": 4},
        caps={"linkedin": 20, "email": 20},
        replies=3, interested=1, pending_reviews=2, channel_errors=1,
    )


class TestSummary:
    def test_text(self):
        assert make_stats().text() == (
            "Daily summary, Mon 05 Oct\n"
            "Sent today: 13 (LinkedIn 9, Email 4)\n"
            "Caps used: 9/20 LinkedIn, 4/20 Email\n"
            "Replies: 3, interested: 1\n"
            "Pending reviews: 2\n"
            "Channel errors: 1"
        )

    def test_as_dict(self):
        data = make_stats().as_dict()
        assert data["total_sent"] == 13 and data["interested"] == 1
        assert data["text"].startswith("Daily summary")

    def test_collect_stats_counts_today(self, db):
        from app.db.models import (
            AttentionItem, AttentionStatus, AttentionType, Company, Contact, Message,
            MessageDirection, MessageStatus, Outreach, ReviewStatus,
        )

        company = Company(name="Acme")
        db.add(company)
        db.flush()
        outreaches = {}
        for index, channel in enumerate(ChannelType):
            contact = Contact(company_id=company.id, name=f"P{index}", email=f"p{index}@x.test")
            db.add(contact)
            db.flush()
            outreaches[channel] = Outreach(contact_id=contact.id, channel=channel)
            db.add(outreaches[channel])
        db.flush()
        old = IN_WINDOW - timedelta(days=30)

        def message(channel, direction, status, at, review=ReviewStatus.PENDING_REVIEW):
            db.add(Message(outreach_id=outreaches[channel].id, direction=direction, content="x",
                           status=status, sent_at=at, created_at=at, review_status=review))

        out, inbound = MessageDirection.OUTBOUND, MessageDirection.INBOUND
        message(ChannelType.LINKEDIN, out, MessageStatus.SENT, old)  # the first send: ramp is over for LinkedIn
        for _ in range(3):
            message(ChannelType.LINKEDIN, out, MessageStatus.SENT, IN_WINDOW)
        for _ in range(2):
            message(ChannelType.EMAIL, out, MessageStatus.SENT, IN_WINDOW)
        message(ChannelType.EMAIL, out, MessageStatus.DRAFTED, IN_WINDOW)  # not sent: not counted
        for _ in range(3):
            message(ChannelType.EMAIL, inbound, MessageStatus.SENT, IN_WINDOW)
        message(ChannelType.EMAIL, inbound, MessageStatus.SENT, old)  # an old reply
        for _ in range(2):
            message(ChannelType.LINKEDIN, out, MessageStatus.APPROVED, IN_WINDOW)  # held for review
        message(ChannelType.LINKEDIN, out, MessageStatus.APPROVED, IN_WINDOW, ReviewStatus.CANCELLED)  # not held
        for kind, status_, at in [
            (AttentionType.INTERESTED, AttentionStatus.OPEN, IN_WINDOW),
            (AttentionType.INTERESTED, AttentionStatus.OPEN, old),
            (AttentionType.QUESTION, AttentionStatus.OPEN, IN_WINDOW),
            (AttentionType.QUESTION, AttentionStatus.RESOLVED, IN_WINDOW),
            (AttentionType.CHANNEL_ERROR, AttentionStatus.OPEN, IN_WINDOW),
            (AttentionType.CHANNEL_ERROR, AttentionStatus.DISMISSED, IN_WINDOW),
        ]:
            db.add(AttentionItem(type=kind, status=status_, payload={}, created_at=at))
        db.commit()

        stats = collect_stats(db, IN_WINDOW)
        assert stats.sent == {"linkedin": 3, "email": 2}
        assert stats.caps == {"linkedin": 40, "email": 20}
        assert (stats.replies, stats.interested, stats.pending_reviews, stats.channel_errors) == (3, 1, 3, 1)
        assert stats.date == "Mon 05 Oct"

    def test_collect_stats_is_four_queries_and_handles_an_empty_database(self, db):
        from sqlalchemy import event

        counted = []

        @event.listens_for(db.get_bind(), "before_cursor_execute")
        def count(*args):  # noqa: ANN002
            counted.append(1)

        try:
            stats = collect_stats(db, IN_WINDOW)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", count)
        assert len(counted) == 4
        assert stats.sent == {"linkedin": 0, "email": 0} and stats.caps == {"linkedin": 20, "email": 20}
        assert (stats.replies, stats.interested, stats.pending_reviews, stats.channel_errors) == (0, 0, 0, 0)

    @pytest.mark.parametrize(
        ("now", "last", "expected"),
        [
            (IN_WINDOW, None, False),
            (EVENING - timedelta(minutes=1), None, False),
            (EVENING, None, True),
            (EVENING, EVENING - timedelta(hours=1), False),
            (EVENING + timedelta(hours=2), EVENING, False),
            (EVENING + timedelta(days=1), EVENING, True),
            (EVENING, IN_WINDOW - timedelta(days=1), True),
        ],
    )
    def test_summary_due(self, now, last, expected):
        assert summary_due(now, last) is expected

    @pytest.mark.asyncio
    async def test_sends_once_and_logs(self, monkeypatch):
        monkeypatch.setattr(reports, "collect_stats", lambda db, now: make_stats())
        db = MagicMock()
        db.scalar.return_value = None
        notify = AsyncMock()
        assert await send_daily_summary_if_due(db, notify, EVENING) is True
        assert notify.await_args.args[0].startswith("Daily summary, Mon 05 Oct")
        row = db.add.call_args.args[0]
        assert row.type == DAILY_SUMMARY and row.payload["total_sent"] == 13
        db.scalar.return_value = EVENING  # already logged today
        assert await send_daily_summary_if_due(db, notify, EVENING + timedelta(hours=1)) is False
        assert notify.await_count == 1

    @pytest.mark.asyncio
    async def test_not_sent_before_8pm(self):
        db = MagicMock()
        db.scalar.return_value = None
        notify = AsyncMock()
        assert await send_daily_summary_if_due(db, notify, IN_WINDOW) is False
        notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_is_not_logged_so_it_retries(self, monkeypatch):
        monkeypatch.setattr(reports, "collect_stats", lambda db, now: make_stats())
        db = MagicMock()
        db.scalar.return_value = None
        notify = AsyncMock(side_effect=RuntimeError("down"))
        assert await send_daily_summary_if_due(db, notify, EVENING) is False
        db.add.assert_not_called()


class TestAPI:
    def test_today(self, monkeypatch):
        monkeypatch.setattr(reports_api, "collect_stats", lambda db, now: make_stats())
        app = FastAPI()
        app.include_router(reports_api.router)
        app.dependency_overrides[get_session] = lambda: MagicMock()
        data = TestClient(app).get("/reports/today").json()
        assert data["sent"] == {"linkedin": 9, "email": 4}
        assert data["caps"]["linkedin"] == 20
        assert data["total_sent"] == 13 and data["pending_reviews"] == 2
        assert data["text"].startswith("Daily summary")


# ─── Send queue integration ────────────────────────────────


class TestQueueIntegration:
    @pytest.mark.asyncio
    async def test_successful_send_is_reported_and_flushed(self):
        from tests.test_send_queue import simple_setup

        h, messages, _, _ = simple_setup(1)
        report = await h.run()
        assert report.sent == 1
        h.reporter.record_send.assert_awaited_once_with(1, ChannelType.EMAIL, "Person 1", "Acme")
        h.reporter.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_dry_run_and_failures_not_reported(self):
        from tests.test_send_queue import simple_setup

        h, _, _, _ = simple_setup(1, dry_run=True)
        await h.run()
        h.reporter.record_send.assert_not_called()
        h, _, _, _ = simple_setup(1)
        h.client.send_email = AsyncMock(side_effect=RuntimeError("boom"))
        await h.run()
        h.reporter.record_send.assert_not_called()
