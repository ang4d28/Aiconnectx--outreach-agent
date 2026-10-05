from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app import runtime_config
from app.api import dashboard
from app.auth.deps import get_current_user
from app.config import settings
from app.db import get_session
from app.db.models import (
    AttentionItem,
    AttentionStatus,
    AttentionType,
    ChannelType,
    MessageDirection,
    MessageStatus,
    OutreachState,
    ReviewStatus,
    Setting,
)

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # Mon 11:30 IST

# Login, sessions, roles and permissions are covered in test_auth.py. These tests call
# the dashboard router directly as a signed-in member.
MEMBER = SimpleNamespace(id=7, role="member")


# ─── Runtime settings ──────────────────────────────────────


class FakeSettingsDb:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.commits = 0

    def get(self, model, key):
        return self.rows.get(key)

    def add(self, obj):
        self.rows[obj.key] = obj

    def commit(self):
        self.commits += 1


@pytest.fixture
def restore_settings(monkeypatch):
    for name in runtime_config.EDITABLE:
        monkeypatch.setattr(settings, name, getattr(settings, name))


class TestRuntimeConfig:
    def test_validation(self):
        clean, errors = runtime_config.validate_changes(
            {"linkedin_daily_cap": "15", "hold_window_minutes": 20, "dry_run": "false",
             "send_window_start": "09:30", "send_window_end": "18:00", "tpo_email": "tpo@college.edu",
             "tpo_phone": "+91 90000 00000", "tpo_name": " Dr. Rao "}
        )
        assert errors == {}
        assert clean["linkedin_daily_cap"] == 15 and clean["dry_run"] is False
        assert clean["tpo_name"] == "Dr. Rao" and clean["hold_window_minutes"] == 20

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("linkedin_daily_cap", 0), ("linkedin_daily_cap", 101), ("linkedin_daily_cap", True),
            ("email_daily_cap", "lots"), ("hold_window_minutes", 0), ("hold_window_minutes", 241),
            ("send_window_start", "9:00"), ("send_window_start", "25:00"), ("dry_run", "maybe"),
            ("tpo_email", "not-an-email"), ("tpo_phone", "call me maybe"), ("tpo_name", "x" * 121),
            ("jwt_secret", "x"), ("global_pause", True),
        ],
    )
    def test_rejects_bad_values_and_unknown_keys(self, name, value):
        _, errors = runtime_config.validate_changes({name: value})
        assert name in errors

    def test_window_must_be_ordered(self, restore_settings):
        _, errors = runtime_config.validate_changes({"send_window_start": "17:00", "send_window_end": "10:00"})
        assert "send_window_end" in errors
        _, errors = runtime_config.validate_changes({"send_window_end": "09:00"})  # start is 10:00
        assert "send_window_end" in errors

    def test_save_then_apply_roundtrip(self, restore_settings):
        db = FakeSettingsDb()
        runtime_config.save_overrides(db, {"hold_window_minutes": 25, "dry_run": False, "tpo_name": "Dr. Rao"})
        assert db.rows["cfg:dry_run"].value == "false" and db.rows["cfg:hold_window_minutes"].value == "25"
        assert settings.hold_window_minutes == 25 and settings.dry_run is False
        settings.hold_window_minutes = 10
        settings.dry_run = True
        applied = runtime_config.apply_overrides(db)
        assert {"hold_window_minutes", "dry_run", "tpo_name"} <= set(applied)
        assert settings.hold_window_minutes == 25 and settings.dry_run is False and settings.tpo_name == "Dr. Rao"

    def test_invalid_saved_value_is_ignored(self, restore_settings):
        db = FakeSettingsDb({"cfg:hold_window_minutes": Setting(key="cfg:hold_window_minutes", value="banana")})
        before = settings.hold_window_minutes
        assert runtime_config.apply_overrides(db) == []
        assert settings.hold_window_minutes == before

    def test_email_cap_setting_limits_the_send_queue(self, restore_settings):
        from app.workers.send_queue import SendQueue

        queue = SendQueue(db=MagicMock(), client=MagicMock(), linkedin=MagicMock(), review=MagicMock(), reporter=MagicMock())
        queue._first_send_at = lambda channel: NOW - timedelta(days=30)
        queue._sent_today = lambda now, channel: 3
        assert queue._capacity(NOW, ChannelType.EMAIL) == 37
        settings.email_daily_cap = 10
        assert queue._capacity(NOW, ChannelType.EMAIL) == 7

    def test_scheduler_applies_overrides_before_each_job(self):
        from app.scheduler import JobMonitor, guarded

        applied = []
        db = MagicMock()
        db.get.return_value = None
        mon = JobMonitor(lambda: db, AsyncMock())
        mon.apply_overrides = lambda: applied.append(1)
        mon.is_channel_disabled = lambda channel: False
        mon.record_success = lambda job: None
        import asyncio

        asyncio.run(guarded("job", AsyncMock(return_value=1), mon=mon))
        assert applied == [1]


# ─── Dashboard endpoints ───────────────────────────────────


def app_with(db):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: MEMBER
    return TestClient(app)


def make_message(message_id=1, status=MessageStatus.DRAFTED, review=ReviewStatus.PENDING_REVIEW, hold=None):
    return SimpleNamespace(
        id=message_id, outreach_id=3, direction=MessageDirection.OUTBOUND, content="Hello",
        edited_text=None, status=status, review_status=review, hold_until=hold, reviewed_at=None,
        whatsapp_message_id=None, sent_at=NOW, created_at=NOW,
    )


def draft_db(message, state=OutreachState.DRAFTED):
    from app.db.models import Company, Contact, Message, Outreach

    objects = {
        (Message, message.id): message,
        (Outreach, 3): SimpleNamespace(id=3, contact_id=4, channel=ChannelType.LINKEDIN, state=state),
        (Contact, 4): SimpleNamespace(id=4, company_id=5, name="Jane Doe"),
        (Company, 5): SimpleNamespace(id=5, name="Acme"),
    }
    db = MagicMock()
    db.get.side_effect = lambda model, key: objects.get((model, key))
    db.scalars.return_value = iter([])
    return db


class TestDraftActions:
    def test_approve_draft_lets_the_queue_start_the_hold(self):
        message = make_message()
        r = app_with(draft_db(message)).post("/dashboard/drafts/1/approve")
        assert r.json()["result"] == "approved"
        assert message.status == MessageStatus.APPROVED and message.hold_until is None

    def test_approve_held_message_releases_it_now(self):
        message = make_message(status=MessageStatus.APPROVED, hold=NOW + timedelta(minutes=8))
        before = datetime.now(timezone.utc)
        r = app_with(draft_db(message)).post("/dashboard/drafts/1/approve")
        after = datetime.now(timezone.utc)
        assert r.json()["result"] == "released"
        assert message.review_status == ReviewStatus.APPROVED
        # released at the real current UTC time (the endpoint uses utc_now, not the fixed NOW)
        assert message.hold_until.tzinfo is not None
        assert before <= message.hold_until <= after
        assert message.reviewed_at == message.hold_until
        assert message.reviewed_by == 7

    def test_approve_reply_draft_releases_it(self):
        message = make_message()
        r = app_with(draft_db(message, state=OutreachState.REPLIED)).post("/dashboard/drafts/1/approve")
        assert r.json()["result"] == "released" and message.status == MessageStatus.APPROVED
        assert message.hold_until is not None

    @pytest.mark.parametrize("status", [MessageStatus.SENT, MessageStatus.REJECTED, MessageStatus.FAILED])
    def test_cannot_approve_finished_messages(self, status):
        assert app_with(draft_db(make_message(status=status))).post("/dashboard/drafts/1/approve").status_code == 409

    def test_cannot_approve_a_cancelled_hold(self):
        message = make_message(status=MessageStatus.APPROVED, review=ReviewStatus.CANCELLED)
        assert app_with(draft_db(message)).post("/dashboard/drafts/1/approve").status_code == 409

    def test_cancel_never_sends(self):
        message = make_message(status=MessageStatus.APPROVED, hold=NOW)
        r = app_with(draft_db(message)).post("/dashboard/drafts/1/cancel")
        assert r.json()["result"] == "cancelled"
        assert message.review_status == ReviewStatus.CANCELLED and message.status == MessageStatus.REJECTED

    def test_cannot_cancel_a_sent_message(self):
        assert app_with(draft_db(make_message(status=MessageStatus.SENT))).post("/dashboard/drafts/1/cancel").status_code == 409

    def test_edit_plain_draft_changes_its_content(self):
        message = make_message()
        r = app_with(draft_db(message)).patch("/dashboard/drafts/1", json={"text": "  New text "})
        assert r.json()["result"] == "edited"
        assert message.content == "New text" and message.edited_text is None

    def test_edit_held_message_restarts_hold_and_previews(self, monkeypatch):
        notify = AsyncMock(return_value="wamid")
        monkeypatch.setattr("app.workers.review.whatsapp_notify", notify)
        monkeypatch.setattr(dashboard, "ReviewService", lambda db: __import__("app.workers.review", fromlist=["x"]).ReviewService(db, notify=notify, clock=lambda: NOW))
        message = make_message(status=MessageStatus.APPROVED, hold=NOW + timedelta(minutes=2))
        r = app_with(draft_db(message)).patch("/dashboard/drafts/1", json={"text": "Better"})
        assert r.json()["result"] == "hold_restarted"
        assert message.edited_text == "Better" and message.review_status == ReviewStatus.EDITED
        assert message.hold_until == NOW + timedelta(minutes=settings.hold_window_minutes)
        assert '"Better"' in notify.await_args.args[0]

    def test_edit_validation_and_missing(self):
        client = app_with(draft_db(make_message()))
        assert client.patch("/dashboard/drafts/1", json={"text": ""}).status_code == 422
        assert client.patch("/dashboard/drafts/1", json={"text": "   "}).status_code == 422
        assert client.patch("/dashboard/drafts/99", json={"text": "x"}).status_code == 404
        sent = app_with(draft_db(make_message(status=MessageStatus.SENT)))
        assert sent.patch("/dashboard/drafts/1", json={"text": "x"}).status_code == 409

    def test_bulk_approve_reports_what_it_skipped(self):
        draft = make_message(1)
        sent = make_message(2, status=MessageStatus.SENT)
        from app.db.models import Message

        db = draft_db(draft)
        base = db.get.side_effect
        db.get.side_effect = lambda model, key: {1: draft, 2: sent}.get(key) if model is Message else base(model, key)
        r = app_with(db).post("/dashboard/drafts/bulk-approve", json={"ids": [1, 2, 3, 1]})
        assert r.json() == {"approved": [1], "skipped": [{"id": 2, "reason": "This message can no longer be approved"},
                                                          {"id": 3, "reason": "not found"}]}

    def test_bulk_approve_limits(self):
        client = app_with(MagicMock())
        assert client.post("/dashboard/drafts/bulk-approve", json={"ids": []}).status_code == 422
        assert client.post("/dashboard/drafts/bulk-approve", json={"ids": list(range(101))}).status_code == 422


class TestStatusAndPause:
    def db(self, flags):
        db = MagicMock()
        db.get.side_effect = lambda model, key: Setting(key=key, value=flags[key]) if key in flags else None
        db.scalars.return_value = iter([
            AttentionItem(id=2, type=AttentionType.CHANNEL_ERROR, status=AttentionStatus.OPEN,
                          payload={"channel": "outlook", "error": "GraphError: 401"}),
        ])
        db.execute.return_value.all.return_value = [(AttentionType.INTERESTED, 2), (AttentionType.QUESTION, 1)]
        return db

    def test_status_lists_disabled_channels_with_reasons(self, monkeypatch):
        monkeypatch.setattr(settings, "dry_run", True)
        flags = {"global_pause": "true", "linkedin_disabled": "true", "linkedin_disabled_reason": "captcha",
                 "outlook_disabled": "true"}
        body = app_with(self.db(flags)).get("/dashboard/status").json()
        assert body["paused"] is True and body["dry_run"] is True
        assert body["disabled_channels"] == [
            {"channel": "linkedin", "reason": "captcha"},
            {"channel": "outlook", "reason": "GraphError: 401"},
        ]
        assert body["open_attention"] == {"interested": 2, "question": 1}

    def test_clean_status(self):
        body = app_with(self.db({})).get("/dashboard/status").json()
        assert body["paused"] is False and body["disabled_channels"] == []

    def test_pause_switch_sets_the_global_flag(self):
        db = FakeSettingsDb()
        db.scalars = MagicMock()
        r = app_with(db).post("/dashboard/pause", json={"paused": True})
        assert r.json() == {"paused": True} and db.rows["global_pause"].value == "true"
        app_with(db).post("/dashboard/pause", json={"paused": False})
        assert db.rows["global_pause"].value == "false"

    def test_enable_channel_clears_flag_counters_and_alerts(self):
        item = AttentionItem(id=1, type=AttentionType.CHANNEL_ERROR, status=AttentionStatus.OPEN,
                             payload={"channel": "linkedin"})
        other = AttentionItem(id=2, type=AttentionType.CHANNEL_ERROR, status=AttentionStatus.OPEN,
                              payload={"channel": "sheets"})
        counter = Setting(key="job_failures:connection_poll", value="3")
        db = FakeSettingsDb({"linkedin_disabled": Setting(key="linkedin_disabled", value="true")})
        db.scalars = MagicMock(side_effect=[iter([counter]), iter([item, other])])
        r = app_with(db).post("/dashboard/channels/linkedin/enable")
        assert r.status_code == 200
        assert db.rows["linkedin_disabled"].value == "false" and db.rows["linkedin_disabled_reason"].value == ""
        assert counter.value == "0"
        assert item.status == AttentionStatus.RESOLVED and other.status == AttentionStatus.OPEN
        assert app_with(db).post("/dashboard/channels/whatsapp/enable").status_code == 404

    def test_channel_keys_match_the_scheduler(self):
        from app.scheduler import DISABLED_KEYS

        assert dashboard.CHANNEL_KEYS == DISABLED_KEYS


class TestOverviewHelpers:
    def test_activity_buckets_by_ist_day(self):
        late = datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc)  # 00:30 IST on the 6th
        rows = dashboard.build_activity(NOW, leads=[NOW, NOW - timedelta(days=1)], sent=[late, NOW],
                                        replies=[NOW - timedelta(days=30)])
        assert len(rows) == 14 and rows[-1]["date"] == "2026-10-05"
        today = rows[-1]
        assert (today["leads"], today["sent"], today["replies"]) == (1, 1, 0)  # `late` is tomorrow in IST
        assert rows[-2]["leads"] == 1
        assert sum(r["replies"] for r in rows) == 0

    def test_funnel_counts(self):
        db = MagicMock()
        db.scalar.side_effect = [120, 90, 60, 20, 5]
        assert dashboard.funnel_counts(db) == [
            {"stage": "Leads", "count": 120}, {"stage": "Drafted", "count": 90},
            {"stage": "Sent", "count": 60}, {"stage": "Replied", "count": 20},
            {"stage": "Interested", "count": 5},
        ]

    def test_overview_assembles_cards(self, monkeypatch):
        from app.workers import reports

        stats = reports.Stats(date="x", sent={"linkedin": 3, "email": 2}, caps={"linkedin": 20, "email": 20},
                              replies=4, interested=1)
        monkeypatch.setattr(reports, "collect_stats", lambda db, now: stats)
        db = MagicMock()
        db.scalar.side_effect = [7, 12] + [10, 8, 6, 4, 2]  # leads today, drafts pending, funnel
        db.scalars.side_effect = [iter([NOW]), iter([NOW]), iter([NOW])]
        body = app_with(db).get("/dashboard/overview").json()
        assert body["cards"] == {"leads_today": 7, "drafts_pending": 12, "sent_today": 5,
                                 "replies_today": 4, "interested_today": 1}
        assert len(body["activity"]) == 14 and body["funnel"][0]["count"] == 10
        assert body["caps"]["limits"] == {"linkedin": 20, "email": 20}


class TestPaginationAndQueries:
    def test_envelope(self):
        db = MagicMock()
        db.scalar.return_value = 45
        outreach = SimpleNamespace(id=1, post_id=None, channel=ChannelType.EMAIL, state=OutreachState.DRAFTED,
                                   sent_at=None, follow_up_due=None, created_at=NOW)
        contact = SimpleNamespace(name="Raj", title="CTO", email="r@x.io", linkedin_url=None)
        db.execute.return_value.all.return_value = [(outreach, contact, SimpleNamespace(name="Beta"), None)]
        body = app_with(db).get("/dashboard/leads", params={"page": 3, "page_size": 10}).json()
        assert (body["total"], body["page"], body["page_size"]) == (45, 3, 10)
        assert body["items"][0] == {
            "id": 1, "person": "Raj", "title": "CTO", "email": "r@x.io", "linkedin_url": None, "company": "Beta",
            "channel": "email", "status": "drafted", "source": "search", "post_url": None, "sent_at": None,
            "follow_up_due": None, "created_at": NOW.isoformat(),
        }

    @pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"status": "bogus"},
                                        {"channel": "fax"}, {"source": "magic"}, {"q": "x" * 101}])
    def test_bad_query_params_rejected(self, params):
        assert app_with(MagicMock()).get("/dashboard/leads", params=params).status_code == 422

    def test_filters_compile_for_postgres(self):
        sql = str(dashboard.leads_statement("drafted", "email", "post", "50%_off").compile(dialect=postgresql.dialect()))
        assert "outreaches.state" in sql and "outreaches.channel" in sql
        assert "outreaches.post_id IS NOT NULL" in sql and "ILIKE" in sql and "ESCAPE" in sql
        assert "LEFT OUTER JOIN posts" in sql

    def test_search_term_is_escaped(self):
        assert dashboard.like_pattern("50%_a\\") == "%50\\%\\_a\\\\%"

    @pytest.mark.parametrize("view", ["awaiting", "held", "sent", "closed", "all"])
    def test_draft_views_compile(self, view):
        sql = str(dashboard.drafts_statement(view).compile(dialect=postgresql.dialect()))
        assert "messages.direction" in sql and "ORDER BY messages.id DESC" in sql

    def test_replies_statement_uses_latest_inbound_per_lead(self):
        sql = str(dashboard.replies_statement().compile(dialect=postgresql.dialect()))
        assert "max(messages.id)" in sql and "GROUP BY messages.outreach_id" in sql

    def test_intent_mapping(self):
        assert dashboard.INTENT_BY_STATE[OutreachState.INTERESTED] == "interested"
        assert dashboard.INTENT_BY_STATE[OutreachState.FUTURE_POOL] == "no_opportunity"
        assert dashboard.INTENT_BY_STATE[OutreachState.UNSUBSCRIBED] == "unsubscribe"

    def test_draft_view_validated(self):
        assert app_with(MagicMock()).get("/dashboard/drafts", params={"view": "nope"}).status_code == 422

    def test_attention_listing(self):
        db = MagicMock()
        db.scalar.return_value = 1
        db.scalars.return_value = iter([AttentionItem(id=4, type=AttentionType.QUESTION, status=AttentionStatus.OPEN,
                                                      payload={"draft": "x"}, created_at=NOW)])
        body = app_with(db).get("/dashboard/attention", params={"type": "question", "status": "all"}).json()
        assert body["total"] == 1 and body["items"][0]["type"] == "question"
        assert app_with(db).get("/dashboard/attention", params={"status": "bogus"}).status_code == 422


class TestSettingsEndpoints:
    @pytest.fixture(autouse=True)
    def isolate(self, restore_settings, monkeypatch):
        monkeypatch.setattr(dashboard, "load_queries", lambda db: ["q1", "q2"])

    def test_get(self):
        body = app_with(MagicMock()).get("/dashboard/settings").json()
        assert set(body["values"]) == set(runtime_config.EDITABLE) and body["search_queries"] == ["q1", "q2"]

    def test_save_applies_immediately(self):
        db = FakeSettingsDb()
        body = app_with(db).put("/dashboard/settings", json={"values": {"hold_window_minutes": 30, "tpo_name": "Dr. Rao"}}).json()
        assert body["values"]["hold_window_minutes"] == 30 and settings.hold_window_minutes == 30
        assert db.rows["cfg:tpo_name"].value == "Dr. Rao"

    def test_invalid_values_return_field_errors_and_save_nothing(self):
        db = FakeSettingsDb()
        r = app_with(db).put("/dashboard/settings", json={"values": {"hold_window_minutes": 0, "jwt_secret": "x"}})
        assert r.status_code == 422
        assert set(r.json()["detail"]["errors"]) == {"hold_window_minutes", "jwt_secret"}
        assert db.rows == {}

    def test_going_live_needs_confirmation(self, monkeypatch):
        monkeypatch.setattr(settings, "dry_run", True)
        db = FakeSettingsDb()
        r = app_with(db).put("/dashboard/settings", json={"values": {"dry_run": False}})
        assert r.status_code == 400 and settings.dry_run is True and db.rows == {}
        r = app_with(db).put("/dashboard/settings", json={"values": {"dry_run": False}, "confirm_live": True})
        assert r.status_code == 200 and settings.dry_run is False

    def test_turning_dry_run_back_on_needs_no_confirmation(self, monkeypatch):
        monkeypatch.setattr(settings, "dry_run", False)
        assert app_with(FakeSettingsDb()).put("/dashboard/settings", json={"values": {"dry_run": True}}).status_code == 200

    def test_search_queries_saved_cleaned_and_deduplicated(self):
        db = FakeSettingsDb()
        r = app_with(db).put("/dashboard/search-queries", json={"queries": [" a ", "b", "a", "  "]})
        assert r.status_code == 200
        assert json.loads(db.rows["search_queries"].value) == ["a", "b"]

    def test_search_queries_validation(self):
        client = app_with(FakeSettingsDb())
        assert client.put("/dashboard/search-queries", json={"queries": []}).status_code == 422
        assert client.put("/dashboard/search-queries", json={"queries": ["   "]}).status_code == 422
        assert client.put("/dashboard/search-queries", json={"queries": ["x" * 301]}).status_code == 422
        assert client.put("/dashboard/search-queries", json={"queries": ["q"] * 31}).status_code == 422

    def test_post_monitor_prefers_saved_queries(self):
        from app.workers.post_monitor import PostMonitor

        db = MagicMock()
        db.get.return_value = Setting(key="search_queries", value=json.dumps(["site:linkedin.com/posts hiring"]))
        assert PostMonitor(db=db)._load_queries() == ["site:linkedin.com/posts hiring"]
        db.get.return_value = Setting(key="search_queries", value="not json")
        assert PostMonitor(db=db)._load_queries()  # falls back to the file or defaults
