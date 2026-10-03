from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import whatsapp_webhook as wh
from app.db.models import (
    ChannelType,
    Company,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    NotificationLog,
    OutreachState,
    Outreach,
    ReviewStatus,
    Setting,
)
from app.workers.review import (
    ReviewCommands,
    ReviewService,
    format_preview,
    hold_until_for,
    next_send_time,
)

IN_WINDOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
NEXT_OPEN = datetime(2026, 10, 6, 4, 30, tzinfo=timezone.utc)  # 10:00 IST next day


def ist(hour: int, minute: int, day: int = 5) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone(timedelta(hours=5, minutes=30)))


class FakeDB:
    def __init__(self, objects=None, scalar_values=None):
        self.objects = dict(objects or {})
        self.added: list = []
        self.commits = 0
        self.closed = False
        self.scalar_values = list(scalar_values or [])

    def get(self, model, key):
        return self.objects.get((model, key))

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, Setting):
            self.objects[(Setting, obj.key)] = obj

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True

    def scalar(self, _stmt):
        return self.scalar_values.pop(0) if self.scalar_values else 0

    def scalars(self, _stmt):
        return iter(self.objects.get("attention", []))


def make_message(status=MessageStatus.APPROVED, review=ReviewStatus.PENDING_REVIEW):
    return SimpleNamespace(
        id=7, outreach_id=3, content="Hi Jane", edited_text=None,
        direction=MessageDirection.OUTBOUND, status=status, review_status=review,
        hold_until=IN_WINDOW + timedelta(minutes=10), reviewed_at=None,
        whatsapp_message_id=None,
    )


def make_db(message=None, **kw):
    message = message or make_message()
    objects = {
        (Message, 7): message,
        (Outreach, 3): SimpleNamespace(id=3, contact_id=4, channel=ChannelType.LINKEDIN,
                                      state=OutreachState.DRAFTED),
        (Contact, 4): SimpleNamespace(id=4, company_id=5, name="Jane Doe"),
        (Company, 5): SimpleNamespace(id=5, name="Acme"),
    }
    objects.update(kw.get("extra", {}))
    return FakeDB(objects, kw.get("scalars")), message


def commands(db, now=IN_WINDOW):
    notify = AsyncMock(return_value="wamid.9")
    return ReviewCommands(session_factory=lambda: db, notify=notify, clock=lambda: now), notify


# ─── Hold timing ───────────────────────────────────────────


class TestHoldTiming:
    def test_inside_window_unchanged(self):
        assert next_send_time(IN_WINDOW) == IN_WINDOW

    def test_after_close_moves_to_next_morning(self):
        assert next_send_time(ist(17, 5)) == NEXT_OPEN

    def test_before_open_moves_to_same_morning(self):
        assert next_send_time(ist(7, 0, day=6)) == NEXT_OPEN

    def test_hold_inside_window(self):
        assert hold_until_for(IN_WINDOW) == IN_WINDOW + timedelta(minutes=10)

    def test_hold_overflowing_close_waits_until_10(self):
        assert hold_until_for(ist(16, 55)) == NEXT_OPEN

    def test_hold_ending_just_before_close_stays(self):
        start = ist(16, 49)
        assert hold_until_for(start) == start + timedelta(minutes=10)


# ─── Preview ───────────────────────────────────────────────


class TestPreview:
    def test_format(self):
        text = format_preview(7, ChannelType.EMAIL, "Jane Doe", "Acme", "Hello", 10)
        assert text == (
            '#7 Email to Jane Doe (Acme)\n"Hello"\nSends in 10 min.\n'
            "Reply: SEND 7 | CANCEL 7 | EDIT 7 <new text>"
        )

    @pytest.mark.asyncio
    async def test_start_hold_sets_fields_and_notifies(self):
        db, message = make_db()
        message.hold_until = None
        notify = AsyncMock(return_value="wamid.1")
        service = ReviewService(db, notify=notify, clock=lambda: IN_WINDOW)
        await service.start_hold(message, ChannelType.LINKEDIN, "Jane Doe", "Acme")
        assert message.hold_until == IN_WINDOW + timedelta(minutes=10)
        assert message.review_status == ReviewStatus.PENDING_REVIEW
        assert message.whatsapp_message_id == "wamid.1"
        preview = notify.await_args.args[0]
        assert preview.startswith('#7 LinkedIn to Jane Doe (Acme)\n"Hi Jane"\nSends in 10 min.')
        assert any(isinstance(obj, NotificationLog) for obj in db.added)

    @pytest.mark.asyncio
    async def test_preview_shows_overflow_wait(self):
        db, message = make_db()
        notify = AsyncMock()
        service = ReviewService(db, notify=notify, clock=lambda: ist(16, 55))
        await service.start_hold(message, ChannelType.EMAIL, "Jane", "Acme")
        assert message.hold_until == NEXT_OPEN
        assert "Sends in 1025 min." in notify.await_args.args[0]

    @pytest.mark.asyncio
    async def test_notify_failure_still_holds(self):
        db, message = make_db()
        service = ReviewService(db, notify=AsyncMock(side_effect=RuntimeError("down")),
                                clock=lambda: IN_WINDOW)
        await service.start_hold(message, ChannelType.EMAIL, "Jane", "Acme")
        assert message.hold_until == IN_WINDOW + timedelta(minutes=10)
        assert db.commits == 1


# ─── Owner commands ────────────────────────────────────────


class TestCommands:
    @pytest.mark.asyncio
    async def test_send_releases_now(self):
        db, message = make_db()
        cmd, _ = commands(db)
        reply = await cmd.send(7)
        assert "released" in reply
        assert message.hold_until == IN_WINDOW
        assert message.review_status == ReviewStatus.APPROVED
        assert message.reviewed_at == IN_WINDOW
        assert db.closed

    @pytest.mark.asyncio
    async def test_cancel_never_sends(self):
        db, message = make_db()
        cmd, _ = commands(db)
        reply = await cmd.cancel(7)
        assert "cancelled" in reply
        assert message.review_status == ReviewStatus.CANCELLED
        assert message.status == MessageStatus.REJECTED

    @pytest.mark.asyncio
    async def test_cancelled_cannot_be_sent(self):
        db, message = make_db(make_message(review=ReviewStatus.CANCELLED))
        cmd, _ = commands(db)
        assert "already cancelled" in await cmd.send(7)
        assert message.review_status == ReviewStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_edit_replaces_text_and_restarts_hold(self):
        db, message = make_db()
        later = IN_WINDOW + timedelta(minutes=8)
        cmd, notify = commands(db, now=later)
        reply = await cmd.edit(7, "Better text")
        assert "Hold restarted" in reply
        assert message.edited_text == "Better text"
        assert message.content == "Hi Jane"
        assert message.review_status == ReviewStatus.EDITED
        assert message.hold_until == later + timedelta(minutes=10)
        preview = notify.await_args.args[0]
        assert preview.startswith('#7 LinkedIn to Jane Doe (Acme)\n"Better text"\nSends in 10 min.')

    @pytest.mark.asyncio
    async def test_already_sent_rejected(self):
        db, _ = make_db(make_message(status=MessageStatus.SENT))
        cmd, notify = commands(db)
        assert "already sent" in await cmd.edit(7, "x")
        notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_message(self):
        db = FakeDB()
        cmd, _ = commands(db)
        assert "not found" in await cmd.cancel(99)

    @pytest.mark.asyncio
    async def test_unapproved_draft_not_queued(self):
        db, _ = make_db(make_message(status=MessageStatus.DRAFTED))
        cmd, _ = commands(db)
        assert "not queued" in await cmd.send(7)

    @pytest.mark.asyncio
    async def test_stop_and_start_toggle_global_pause(self):
        db = FakeDB()
        cmd, _ = commands(db)
        assert "Paused" in await cmd.stop()
        assert db.get(Setting, "global_pause").value == "true"
        assert "Resumed" in await cmd.start()
        assert db.get(Setting, "global_pause").value == "false"

    @pytest.mark.asyncio
    async def test_status_returns_state_and_daily_summary(self, monkeypatch):
        from app.config import settings
        from app.workers import reports

        monkeypatch.setattr(settings, "dry_run", False)
        stats = reports.Stats(date="Mon 05 Oct", sent={"linkedin": 1, "email": 2},
                              caps={"linkedin": 20, "email": 20}, pending_reviews=3)
        monkeypatch.setattr(reports, "collect_stats", lambda db, now: stats)
        db = FakeDB({(Setting, "global_pause"): Setting(key="global_pause", value="true")})
        cmd, _ = commands(db)
        reply = await cmd.status()
        assert reply.startswith("Status: paused.\n")
        assert reply.endswith(stats.text())

    @pytest.mark.asyncio
    async def test_webhook_dispatch_reaches_review_commands(self):
        db, message = make_db()
        cmd, _ = commands(db)
        reply = await wh.dispatch(wh.parse_command("CANCEL 7"), cmd)
        assert "cancelled" in reply
        assert message.review_status == ReviewStatus.CANCELLED
