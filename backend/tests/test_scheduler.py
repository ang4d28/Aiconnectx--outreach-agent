from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import scheduler
from app.channels.linkedin import LinkedInBlocked
from app.channels.outlook import GraphError
from app.config import settings
from app.db.models import (
    AttentionItem,
    AttentionType,
    ChannelType,
    OutreachState,
    PostStatus,
    Setting,
)
from app.scheduler import (
    FAILURE_THRESHOLD,
    JobMonitor,
    build_scheduler,
    build_specs,
    classify_channel,
    guarded,
    prune_stale_jobs,
    window_hours,
)
from app.workers.pipeline import Pipeline
from app.workers.post_monitor import PostMonitor

IN_WINDOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
NIGHT = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)  # 01:30 IST


def triggers():
    return {spec.id: str(spec.trigger) for spec in build_specs()}


# ─── Schedule ──────────────────────────────────────────────


class TestSchedule:
    def test_all_jobs_present(self):
        assert set(triggers()) == {
            "post_monitor", "company_finder", "send_queue", "release_held", "connection_poll",
            "replies", "linkedin_session", "followups", "sheets_sync", "daily_summary",
        }

    def test_triggers_match_the_plan(self):
        t = triggers()
        assert t["post_monitor"] == "interval[2:30:00]"
        assert t["company_finder"] == "cron[hour='8', minute='0']"
        assert t["send_queue"] == "cron[hour='10-16', minute='*/10']"
        assert t["release_held"] == "cron[hour='10-16', minute='*']"
        assert t["connection_poll"] == "cron[hour='10-16/2', minute='0']"
        assert t["replies"] == "cron[hour='10-16', minute='0,30']"
        assert t["linkedin_session"] == "interval[6:00:00]"
        assert t["followups"] == "cron[minute='0']"
        assert t["sheets_sync"] == "cron[minute='15,45']"
        assert t["daily_summary"] == "cron[hour='20', minute='0']"

    def test_all_jobs_run_in_ist(self):
        assert all(str(spec.trigger.timezone) == "Asia/Kolkata" for spec in build_specs())

    @pytest.mark.parametrize(("start", "end", "expected"), [
        ("10:00", "17:00", (10, 16)), ("09:00", "18:30", (9, 18)), ("10:00", "10:30", (10, 10)),
    ])
    def test_window_hours_follow_settings(self, monkeypatch, start, end, expected):
        monkeypatch.setattr(settings, "send_window_start", start)
        monkeypatch.setattr(settings, "send_window_end", end)
        assert window_hours() == expected

    @pytest.mark.asyncio
    async def test_jobs_have_safe_defaults(self):
        sched = build_scheduler()
        sched.start(paused=True)
        try:
            job = sched.get_job("send_queue")
            assert job.coalesce is True and job.max_instances == 1
            assert job.func_ref == "app.scheduler:job_send_queue"
            assert all(j.next_run_time is not None for j in sched.get_jobs())
        finally:
            sched.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_postgres_style_store_roundtrip_and_prune(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'jobs.db'}"
        first = build_scheduler(url)
        first.start(paused=True)
        first.add_job("app.scheduler:job_followups", "interval", hours=1, id="retired_job")
        first.shutdown(wait=False)

        second = build_scheduler(url)  # restart: same jobs reloaded, not duplicated
        second.start(paused=True)
        try:
            assert len(second.get_jobs()) == len(build_specs()) + 1
            assert prune_stale_jobs(second) == ["retired_job"]
            assert {j.id for j in second.get_jobs()} == {s.id for s in build_specs()}
        finally:
            second.shutdown(wait=False)

    def test_check_mode_runs_without_a_database(self, capsys):
        asyncio.run(scheduler.check())
        out = capsys.readouterr().out
        assert "Mode: " in out and "send_queue" in out and "daily_summary" in out


# ─── Failure handling ──────────────────────────────────────


class FakeSessions:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else {}
        self.added: list = []
        self.commits = 0

    def __call__(self):
        return self

    def get(self, model, key):
        return self.rows.get(key)

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, Setting):
            self.rows[obj.key] = obj

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def attention(self):
        return [o for o in self.added if isinstance(o, AttentionItem)]


def make_monitor(rows=None):
    sessions = FakeSessions(rows)
    notify = AsyncMock()
    return JobMonitor(sessions, notify), sessions, notify


class TestFailureHandling:
    def test_classify_channel(self):
        assert classify_channel(LinkedInBlocked("captcha"), None) == "linkedin"
        assert classify_channel(GraphError(500, "x"), None) == "outlook"
        assert classify_channel(RuntimeError("x"), "sheets") == "sheets"
        assert classify_channel(RuntimeError("x"), None) is None

    @pytest.mark.asyncio
    async def test_first_failure_creates_attention_and_alert_only(self):
        mon, db, notify = make_monitor()
        await mon.record_failure("sheets_sync", RuntimeError("quota"), "sheets")
        item = db.attention()[0]
        assert item.type == AttentionType.CHANNEL_ERROR
        assert item.payload["job"] == "sheets_sync" and item.payload["channel"] == "sheets"
        assert item.payload["disabled"] is False and "quota" in item.payload["error"]
        assert "sheets_sync failed" in notify.await_args.args[0]
        assert "sheets_disabled" not in db.rows

    @pytest.mark.asyncio
    async def test_repeats_are_quiet_until_the_threshold(self):
        mon, db, notify = make_monitor()
        for _ in range(FAILURE_THRESHOLD - 1):
            await mon.record_failure("sheets_sync", RuntimeError("quota"), "sheets")
        assert len(db.attention()) == 1 and notify.await_count == 1

    @pytest.mark.asyncio
    async def test_threshold_disables_channel_and_escalates(self):
        mon, db, notify = make_monitor()
        for _ in range(FAILURE_THRESHOLD):
            await mon.record_failure("sheets_sync", RuntimeError("quota"), "sheets")
        assert db.rows["sheets_disabled"].value == "true"
        assert db.attention()[-1].payload["disabled"] is True
        assert "sheets channel is now disabled" in notify.await_args.args[0]
        assert mon.is_channel_disabled("sheets") is True

    @pytest.mark.asyncio
    async def test_outlook_errors_disable_email_from_any_job(self):
        mon, db, _ = make_monitor()
        for _ in range(FAILURE_THRESHOLD):
            await mon.record_failure("send_queue", GraphError(401, "denied"), None)
        assert db.rows["outlook_disabled"].value == "true"
        assert db.attention()[-1].payload["channel"] == "outlook"

    @pytest.mark.asyncio
    async def test_unclassified_job_failure_alerts_but_disables_nothing(self):
        mon, db, notify = make_monitor()
        for _ in range(FAILURE_THRESHOLD + 1):
            await mon.record_failure("post_monitor", RuntimeError("boom"), None)
        assert db.attention()[0].payload["channel"] == "scheduler"
        assert not [k for k in db.rows if k.endswith("_disabled")]
        assert notify.await_count == 1

    @pytest.mark.asyncio
    async def test_alert_failure_never_raises(self):
        mon, db, notify = make_monitor()
        notify.side_effect = RuntimeError("whatsapp down")
        await mon.record_failure("sheets_sync", RuntimeError("x"), "sheets")
        assert len(db.attention()) == 1

    @pytest.mark.asyncio
    async def test_success_resets_the_counter(self):
        mon, db, _ = make_monitor()
        await mon.record_failure("replies", RuntimeError("x"), None)
        mon.record_success("replies")
        assert db.rows["job_failures:replies"].value == "0"
        await mon.record_failure("replies", RuntimeError("x"), None)
        assert len(db.attention()) == 2  # a fresh first failure escalates again

    def test_success_with_no_failures_writes_nothing(self):
        mon, db, _ = make_monitor()
        mon.record_success("replies")
        assert db.commits == 0

    def test_unknown_channel_is_never_disabled(self):
        mon, _, _ = make_monitor()
        assert mon.is_channel_disabled(None) is False
        assert mon.is_channel_disabled("whatsapp") is False


class TestGuarded:
    def monitor(self, disabled=False):
        mon = MagicMock()
        mon.is_channel_disabled.return_value = disabled
        mon.record_failure = AsyncMock()
        return mon

    @pytest.mark.asyncio
    async def test_runs_body_and_records_success(self):
        mon = self.monitor()
        body = AsyncMock(return_value={"ok": 1})
        assert await guarded("j", body, mon=mon) == {"ok": 1}
        mon.record_success.assert_called_once_with("j")

    @pytest.mark.asyncio
    async def test_exception_is_contained_and_recorded(self):
        mon = self.monitor()
        error = RuntimeError("boom")
        assert await guarded("j", AsyncMock(side_effect=error), channel="sheets", mon=mon) is None
        mon.record_failure.assert_awaited_once_with("j", error, "sheets")
        mon.record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_channel_skips_body(self):
        body = AsyncMock()
        await guarded("j", body, channel="linkedin", mon=self.monitor(disabled=True))
        body.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_only_jobs_skip_outside_window(self, monkeypatch):
        body = AsyncMock(return_value=1)
        monkeypatch.setattr(scheduler, "utc_now", lambda: NIGHT)
        await guarded("j", body, window_only=True, mon=self.monitor())
        body.assert_not_called()
        monkeypatch.setattr(scheduler, "utc_now", lambda: IN_WINDOW)
        await guarded("j", body, window_only=True, mon=self.monitor())
        body.assert_awaited_once()


class TestJobs:
    @pytest.mark.asyncio
    async def test_send_jobs_use_the_right_hold_phase(self, monkeypatch):
        calls = []

        async def fake_run(start_holds):
            calls.append(start_holds)

        async def passthrough(job, body, **kwargs):
            await body()

        monkeypatch.setattr(scheduler, "run_send_queue", fake_run)
        monkeypatch.setattr(scheduler, "guarded", passthrough)
        await scheduler.job_send_queue()
        await scheduler.job_release_held()
        assert calls == [True, False]

    @pytest.mark.asyncio
    async def test_release_skips_while_a_queue_run_is_active(self, monkeypatch):
        calls = []
        monkeypatch.setattr(scheduler, "guarded", AsyncMock(side_effect=lambda *a, **k: calls.append(a[0])))
        async with scheduler.send_lock():
            await scheduler.job_release_held()
        assert calls == []

    @pytest.mark.asyncio
    async def test_sheets_job_reports_failure_so_it_escalates(self, monkeypatch):
        from app.channels import sheets

        monkeypatch.setattr(sheets, "sheets_enabled", lambda: True)
        monkeypatch.setattr(scheduler, "in_session", AsyncMock(return_value=None))
        with pytest.raises(RuntimeError):
            await scheduler.run_sheets_sync()
        monkeypatch.setattr(sheets, "sheets_enabled", lambda: False)
        assert "skipped" in await scheduler.run_sheets_sync()


# ─── Company finder ────────────────────────────────────────


def make_post(post_id=1, company_id=3, score=0.9):
    return SimpleNamespace(id=post_id, company_id=company_id, relevance_score=score, status=PostStatus.NEW)


def make_contact(linkedin="https://linkedin.com/in/a", email=None, unsub=False):
    return SimpleNamespace(id=7, linkedin_url=linkedin, email=email, is_unsubscribed=unsub)


class PipelineHarness:
    def __init__(self, posts, contact=None, existing=False, drafted=2):
        self.db = MagicMock()
        self.targeting = MagicMock()
        self.targeting.pick_target = AsyncMock(return_value=contact)
        self.drafter = MagicMock()
        self.drafter.run_once = AsyncMock(return_value=drafted)
        self.pipeline = Pipeline(db=self.db, targeting=self.targeting, drafter=self.drafter)
        self.pipeline._new_posts = lambda: posts
        self.created: list = []
        self.statuses: dict = {}
        self.existing = existing
        from app.workers import pipeline as module

        self.patches = [
            (module.repository, "create_outreach", lambda db, data: self.created.append(data)),
            (module.repository, "get_outreaches_by_contact",
             lambda db, cid, **kw: [object()] if self.existing else []),
            (module.repository, "update_post", lambda db, post, data: self.statuses.update({post.id: data.status})),
        ]

    async def run(self, monkeypatch):
        for obj, name, fn in self.patches:
            monkeypatch.setattr(obj, name, fn)
        return await self.pipeline.run_once()


class TestPipeline:
    @pytest.mark.asyncio
    async def test_post_becomes_enriched_outreach_on_linkedin(self, monkeypatch):
        h = PipelineHarness([make_post()], make_contact())
        report = await h.run(monkeypatch)
        assert (report.targeted, report.drafted) == (1, 2)
        outreach = h.created[0]
        assert outreach.channel == ChannelType.LINKEDIN and outreach.state == OutreachState.ENRICHED
        assert (outreach.contact_id, outreach.post_id) == (7, 1)
        assert h.statuses[1] == PostStatus.PROCESSED

    @pytest.mark.asyncio
    async def test_email_only_contact_uses_email(self, monkeypatch):
        h = PipelineHarness([make_post()], make_contact(linkedin=None, email="hr@acme.com"))
        await h.run(monkeypatch)
        assert h.created[0].channel == ChannelType.EMAIL

    @pytest.mark.asyncio
    @pytest.mark.parametrize("post,contact", [
        (make_post(company_id=None), make_contact()),
        (make_post(), None),
        (make_post(), make_contact(unsub=True)),
        (make_post(), make_contact(linkedin=None, email=None)),
    ])
    async def test_unusable_posts_are_skipped(self, monkeypatch, post, contact):
        h = PipelineHarness([post], contact)
        report = await h.run(monkeypatch)
        assert report.skipped == 1 and h.created == []
        assert h.statuses[post.id] == PostStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_contact_already_in_pipeline_gets_no_second_outreach(self, monkeypatch):
        h = PipelineHarness([make_post()], make_contact(), existing=True)
        report = await h.run(monkeypatch)
        assert report.skipped == 1 and h.created == []
        assert h.statuses[1] == PostStatus.PROCESSED

    @pytest.mark.asyncio
    async def test_targeting_error_leaves_post_for_retry(self, monkeypatch):
        h = PipelineHarness([make_post(1), make_post(2)], make_contact())
        h.targeting.pick_target = AsyncMock(side_effect=[RuntimeError("search down"), make_contact()])
        report = await h.run(monkeypatch)
        assert (report.failed, report.targeted) == (1, 1)
        assert 1 not in h.statuses and h.statuses[2] == PostStatus.PROCESSED
        h.db.rollback.assert_called_once()


class TestPostMonitorCompany:
    def monitor(self):
        return PostMonitor(db=MagicMock())

    def test_existing_company_reused(self, monkeypatch):
        from app.workers import post_monitor as module

        create = MagicMock()
        monkeypatch.setattr(module.repository, "get_company_by_name", lambda db, n: SimpleNamespace(id=4))
        monkeypatch.setattr(module.repository, "create_company", create)
        assert self.monitor()._resolve_company(" Acme ") == 4
        create.assert_not_called()

    def test_new_company_created(self, monkeypatch):
        from app.workers import post_monitor as module

        monkeypatch.setattr(module.repository, "get_company_by_name", lambda db, n: None)
        monkeypatch.setattr(module.repository, "create_company",
                            lambda db, data: SimpleNamespace(id=9, name=data.name))
        assert self.monitor()._resolve_company("Beta Labs") == 9

    def test_no_name_no_company(self):
        assert self.monitor()._resolve_company(None) is None
        assert self.monitor()._resolve_company("  ") is None

    def test_db_error_is_contained(self, monkeypatch):
        from app.workers import post_monitor as module

        def boom(db, name):
            raise RuntimeError("db down")

        monkeypatch.setattr(module.repository, "get_company_by_name", boom)
        monitor = self.monitor()
        assert monitor._resolve_company("Acme") is None
        monitor.db.rollback.assert_called_once()


# ─── Disabled Outlook channel inside the workers ───────────


class TestDisabledEmail:
    @pytest.mark.asyncio
    async def test_send_queue_skips_email_when_outlook_disabled(self):
        from tests.test_send_queue import simple_setup

        h, messages, _, _ = simple_setup(1, settings_map={"outlook_disabled": "true"})
        report = await h.run()
        assert report.skipped == 1 and report.sent == 0
        h.client.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_release_only_run_does_not_start_holds(self):
        from tests.test_send_queue import QueueHarness, make_contact, make_message, make_outreach

        message = make_message(1, 1, held=False)
        h = QueueHarness([message], {1: make_outreach(1)}, {1: make_contact(1)})
        for p in h.patches:
            p.start()
        try:
            report = await h.queue.run_once(start_holds=False)
        finally:
            for p in h.patches:
                p.stop()
        assert report.held == 0 and message.hold_until is None
        h.review.start_hold.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_worker_skips_email_poll_and_connection_check_on_request(self):
        from tests.test_linkedin import TestReplyIntegration

        client = MagicMock(available=True)
        client.read_new_replies = AsyncMock(return_value=[])
        client.is_connected = AsyncMock()
        worker, _ = TestReplyIntegration().worker(client, [SimpleNamespace(
            id=1, contact_id=2, state=OutreachState.CONNECT_SENT, updated_at=None)])
        worker._setting = {"outlook_disabled": "true"}.get
        await worker.run_once(check_connections=False)
        worker.channels[ChannelType.EMAIL].read_new_replies.assert_not_called()
        client.is_connected.assert_not_called()
        client.read_new_replies.assert_awaited_once()
