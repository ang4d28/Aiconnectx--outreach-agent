"""`python -m app.cli run-job` and the allowlist-addressed demo leads from scripts/seed_demo.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import cli, scheduler
from app.config import settings
from app.db import repository
from app.db.models import ChannelType, Outreach, OutreachState, Post, PostStatus
from app.workers.pipeline import PipelineReport

_SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_helpers", _SEED)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

ME = "me@test.com"


class TestRunJob:
    @pytest.fixture(autouse=True)
    def _no_overrides(self, monkeypatch):
        monkeypatch.setattr(scheduler.JobMonitor, "apply_overrides", lambda self: None)

    @pytest.mark.parametrize(
        "name, target, args",
        [
            ("post_monitor", "run_post_monitor", ()),
            ("send_queue", "run_send_queue", (True,)),
            ("release_held", "run_send_queue", (False,)),
            ("replies", "run_replies", ()),
            ("followups", "run_followups", ()),
            ("sheets", "run_sheets_sync", ()),
        ],
    )
    def test_runs_the_scheduler_body_once(self, monkeypatch, capsys, name, target, args):
        body = AsyncMock(return_value={"sent": 2, "blocked": 1})
        monkeypatch.setattr(scheduler, target, body)
        assert cli.main(["run-job", name]) == 0
        body.assert_awaited_once_with(*args)
        assert f"OK {name}: sent=2, blocked=1" in capsys.readouterr().out

    def test_prints_dataclass_counts(self, monkeypatch, capsys):
        monkeypatch.setattr(scheduler, "run_post_monitor", AsyncMock(return_value=PipelineReport(drafted=3)))
        assert cli.main(["run-job", "post_monitor"]) == 0
        assert "targeted=0, skipped=0, failed=0, drafted=3" in capsys.readouterr().out

    def test_error_is_printed_and_exit_code_is_1(self, monkeypatch, capsys):
        monkeypatch.setattr(scheduler, "run_replies", AsyncMock(side_effect=RuntimeError("Graph 403")))
        assert cli.main(["run-job", "replies"]) == 1
        assert "FAILED replies: RuntimeError: Graph 403" in capsys.readouterr().err

    def test_failure_does_not_touch_the_failure_counters(self, monkeypatch):
        monkeypatch.setattr(scheduler, "run_replies", AsyncMock(side_effect=RuntimeError("x")))
        record = AsyncMock()
        monkeypatch.setattr(scheduler.JobMonitor, "record_failure", record)
        cli.main(["run-job", "replies"])
        record.assert_not_called()

    def test_unknown_job_is_rejected(self):
        with pytest.raises(SystemExit):
            cli.main(["run-job", "daily_summary"])

    def test_enrich_and_draft_run_their_own_halves(self, monkeypatch, db, capsys):
        from app.workers.drafter import Drafter
        from app.workers.pipeline import Pipeline

        async def in_session(work):
            return await work(db)

        monkeypatch.setattr(scheduler, "in_session", in_session)
        process = AsyncMock(return_value="targeted")
        monkeypatch.setattr(Pipeline, "process_post", process)
        monkeypatch.setattr(Pipeline, "_new_posts", lambda self: [Post(id=1), Post(id=2)])
        monkeypatch.setattr(Drafter, "run_once", AsyncMock(return_value=3))

        assert cli.main(["run-job", "enrich"]) == 0
        assert "OK enrich: targeted=2, skipped=0, failed=0" in capsys.readouterr().out
        assert cli.main(["run-job", "draft"]) == 0
        assert "OK draft: 3" in capsys.readouterr().out


class TestSeedLeads:
    def test_refuses_with_empty_allowlist(self, db):
        with pytest.raises(SystemExit, match="SEND_ALLOWLIST is empty"):
            seed_demo.seed_leads(db, set())
        assert db.query(Outreach).count() == 0

    def test_main_refuses_with_empty_allowlist(self, monkeypatch):
        monkeypatch.setattr(settings, "dry_run", True)
        monkeypatch.setattr(settings, "send_allowlist", "")
        with pytest.raises(SystemExit, match="SEND_ALLOWLIST is empty"):
            seed_demo.main([])

    def test_three_enriched_email_leads_with_posts(self, db):
        leads = seed_demo.seed_leads(db, {ME})
        assert len(leads) == 3
        for outreach, _ in leads:
            assert outreach.state == OutreachState.ENRICHED
            assert outreach.channel == ChannelType.EMAIL
            post = repository.get_post(db, outreach.post_id)
            assert post.status == PostStatus.PROCESSED and post.raw_text and post.roles
            assert post.company_id == repository.get_contact(db, outreach.contact_id).company_id

    def test_single_address_is_plus_addressed(self, db):
        leads = seed_demo.seed_leads(db, {ME})
        emails = [repository.get_contact(db, o.contact_id).email for o, _ in leads]
        assert emails == [ME, "me+demo2@test.com", "me+demo3@test.com"]
        assert [listed for _, listed in leads] == [True, False, False]

    def test_uses_listed_addresses_when_there_are_three(self, db):
        listed = {"a@test.com", "b@test.com", "c@test.com"}
        leads = seed_demo.seed_leads(db, listed)
        emails = {repository.get_contact(db, o.contact_id).email for o, _ in leads}
        assert emails == listed and all(flag for _, flag in leads)

    def test_idempotent(self, db):
        first = seed_demo.seed_leads(db, {ME})
        second = seed_demo.seed_leads(db, {ME})
        assert [o.id for o, _ in first] == [o.id for o, _ in second]
        assert db.query(Outreach).count() == 3
        assert db.query(Post).count() == 3
