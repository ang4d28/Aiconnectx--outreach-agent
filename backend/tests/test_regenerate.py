"""Regenerate and restore drafts (API), `run-job draft --force` (CLI) and `seed_demo.py --reset`."""

from __future__ import annotations

import importlib.util
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import cli, scheduler
from app.api import drafts as drafts_api
from app.api.inquiries import RateLimiter
from app.api.main import api_router
from app.auth import oauth, security
from app.config import settings
from app.db import get_session, repository
from app.db.models import (
    AttentionItem,
    AttentionStatus,
    AuthProvider,
    Company,
    Contact,
    DraftVersion,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    Post,
    ReviewStatus,
    User,
    UserRole,
)
from app.llm.client import LLMError
from app.workers import drafter, review
from app.workers.drafter import DraftedMessage, Drafter
from app.workers.send_queue import utc_now

_SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_regenerate", _SEED)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}
ME = "me@test.com"

OLD = (
    "I saw your hiring push and wanted to introduce Universal AI University's B.Tech AI and ML students. "
    "We would value a short call this week to learn what your team needs and whether a structured "
    "partnership through an MoU could help both sides. Would you be open to a fifteen minute conversation?"
)
NEW = (
    "Your hiring plans caught our attention at Universal AI University, where our AI and ML students are "
    "ready for internships and entry level roles. The AI ConnectX team would like to understand what your "
    "engineers need and see whether an MoU could give both sides a simple, steady route to good candidates. "
    "Could we find fifteen minutes for a call this week?"
)
BAD = "I am [Your Name] from AI ConnectX and would love to talk. " + " ".join(["filler"] * 55)


class FakeLLM:
    def __init__(self, *replies: DraftedMessage | Exception):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete_json(self, prompt, schema):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def use_llm(monkeypatch, *replies) -> FakeLLM:
    llm = FakeLLM(*replies)
    monkeypatch.setattr(drafter, "llm_client", llm)
    return llm


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """Cheap password hashing, the sender configured, no WhatsApp, a fresh rate limiter."""
    from argon2 import PasswordHasher

    monkeypatch.setattr(security, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    security._dummy_hash.cache_clear()
    monkeypatch.setattr(settings, "sender_name", "Asha Verma")
    monkeypatch.setattr(settings, "sender_title", "Outreach Coordinator")
    monkeypatch.setattr(settings, "sender_contact", "asha@connectx.test")
    monkeypatch.setattr(settings, "send_allowlist", ME)
    notify = AsyncMock(return_value="wamid.1")
    monkeypatch.setattr(review.ReviewService.__init__, "__defaults__", (notify, utc_now))
    monkeypatch.setattr(drafts_api, "regenerate_limiter", RateLimiter(limit=10, window=60))
    drafts_api.JOBS.clear()
    yield notify
    security._dummy_hash.cache_clear()


@pytest.fixture
def notify(quiet):
    return quiet


@pytest.fixture
def app(db):
    application = FastAPI()
    oauth.install_session_middleware(application)
    application.include_router(api_router)
    application.dependency_overrides[get_session] = lambda: db
    return application


def user_client(app, db, role: UserRole) -> tuple[TestClient, User]:
    user = User(
        email=f"{role.value}@example.test",
        name=role.value,
        role=role,
        provider=AuthProvider.PASSWORD,
        password_hash=security.hash_password("correct horse battery"),
    )
    db.add(user)
    db.commit()
    http = TestClient(app, follow_redirects=False, headers=CSRF)
    token, _ = security.create_session_token(user)
    http.cookies.set(security.SESSION_COOKIE, token)
    return http, user


@pytest_asyncio.fixture
async def drafts(db, monkeypatch):
    """The three demo leads with a DRAFTED message each (text OLD)."""
    seed_demo.seed_leads(db, {ME})
    use_llm(monkeypatch, DraftedMessage(subject="Student partnership", message=OLD))
    assert await Drafter(db=db).run_once() == 3
    return list(db.scalars(select(Message).order_by(Message.id)))


def body_of(message: Message) -> str:
    return message.edited_text or message.content


class TestRegenerate:
    @pytest.mark.asyncio
    async def test_replaces_the_text_keeps_a_version_and_records_who(self, app, db, drafts, monkeypatch):
        http, user = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh subject here", message=NEW))
        target = drafts[0]
        old_text = target.content

        response = http.post(f"/api/drafts/{target.id}/regenerate", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert NEW in body["text"] and OLD not in body["text"]
        assert body["has_previous"] is True and body["regenerated_at"]

        db.refresh(target)
        assert NEW in target.content and target.subject == "Fresh subject here"
        assert target.review_status == ReviewStatus.PENDING_REVIEW
        assert target.reviewed_by == user.id and target.regenerated_at is not None
        version = db.scalars(select(DraftVersion)).one()
        assert version.draft_id == target.id and version.text == old_text and version.created_by == user.id
        assert version.subject == "Student partnership"
        # the other drafts are untouched
        assert all(OLD in m.content for m in drafts[1:])

    @pytest.mark.asyncio
    async def test_signature_is_rebuilt_from_config_and_greeting_is_added(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        monkeypatch.setattr(settings, "sender_name", "Ravi Kumar")
        http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={})
        db.refresh(drafts[0])
        assert drafts[0].content.startswith("Hi Meera,\n\n")
        assert drafts[0].content.endswith("Regards,\nRavi Kumar\nOutreach Coordinator, AI ConnectX\nasha@connectx.test")

    @pytest.mark.asyncio
    async def test_instruction_reaches_the_prompt_and_is_trimmed(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        llm = use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={"instruction": "  make it   shorter "})
        assert "OWNER'S STYLE REQUEST" in llm.prompts[0] and "make it shorter" in llm.prompts[0]
        plain = use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        http.post(f"/api/drafts/{drafts[1].id}/regenerate", json={})
        assert "OWNER'S STYLE REQUEST" not in plain.prompts[0]
        assert http.post(f"/api/drafts/{drafts[1].id}/regenerate", json={"instruction": "x" * 201}).status_code == 422

    @pytest.mark.asyncio
    async def test_restarts_the_hold_and_the_preview_when_it_was_held(self, app, db, drafts, monkeypatch, notify):
        http, _ = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        held = drafts[0]
        old_hold = utc_now() + timedelta(minutes=2)
        held.status, held.hold_until, held.review_status = MessageStatus.APPROVED, old_hold, ReviewStatus.EDITED
        held.edited_text = "owner edit"
        db.commit()

        body = http.post(f"/api/drafts/{held.id}/regenerate", json={}).json()
        db.refresh(held)
        assert held.status == MessageStatus.APPROVED and held.review_status == ReviewStatus.PENDING_REVIEW
        assert held.hold_until > old_hold and held.edited_text is None
        notify.assert_awaited_once()  # a new WhatsApp preview
        assert NEW in notify.await_args.args[0]
        assert body["hold_until"] is not None
        assert db.scalars(select(DraftVersion)).one().text == "owner edit"  # the edited text is what is kept

    @pytest.mark.asyncio
    async def test_a_draft_not_yet_held_stays_without_a_hold(self, app, db, drafts, monkeypatch, notify):
        http, _ = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={})
        db.refresh(drafts[0])
        assert drafts[0].status == MessageStatus.DRAFTED and drafts[0].hold_until is None
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [MessageStatus.SENT, MessageStatus.REJECTED, MessageStatus.FAILED, MessageStatus.BLOCKED])
    async def test_sent_or_closed_messages_return_409_and_are_untouched(self, app, db, drafts, monkeypatch, status):
        http, _ = user_client(app, db, UserRole.ADMIN)
        llm = use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        drafts[0].status = status
        db.commit()
        response = http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={})
        assert response.status_code == 409
        assert llm.prompts == []  # no LLM call, no cost
        db.refresh(drafts[0])
        assert OLD in drafts[0].content and db.query(DraftVersion).count() == 0

    @pytest.mark.asyncio
    async def test_a_message_being_released_is_409(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.ADMIN)
        llm = use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        released = drafts[0]
        released.status, released.review_status = MessageStatus.APPROVED, ReviewStatus.APPROVED
        released.hold_until = utc_now()
        expired = drafts[1]
        expired.status, expired.review_status = MessageStatus.APPROVED, ReviewStatus.PENDING_REVIEW
        expired.hold_until = utc_now() - timedelta(minutes=1)  # the queue sends it on its next run
        db.commit()
        for message in (released, expired):
            assert http.post(f"/api/drafts/{message.id}/regenerate", json={}).status_code == 409
        assert llm.prompts == []

    @pytest.mark.asyncio
    async def test_sent_while_the_llm_was_working_is_409_and_nothing_is_written(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        target = drafts[0]

        class SendsMeanwhile(FakeLLM):
            async def complete_json(self, prompt, schema):
                target.status = MessageStatus.SENT  # the queue got there first
                db.commit()
                return await super().complete_json(prompt, schema)

        monkeypatch.setattr(drafter, "llm_client", SendsMeanwhile(DraftedMessage(subject="Fresh", message=NEW)))
        assert http.post(f"/api/drafts/{target.id}/regenerate", json={}).status_code == 409
        db.refresh(target)
        assert OLD in target.content and db.query(DraftVersion).count() == 0

    @pytest.mark.asyncio
    async def test_conversation_replies_are_not_regenerated(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.ADMIN)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        outreach = db.get(Outreach, drafts[0].outreach_id)
        outreach.state = OutreachState.REPLIED
        db.commit()
        assert http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={}).status_code == 409

    @pytest.mark.asyncio
    async def test_unknown_draft_is_404(self, app, db, drafts):
        http, _ = user_client(app, db, UserRole.ADMIN)
        assert http.post("/api/drafts/9999/regenerate", json={}).status_code == 404

    @pytest.mark.asyncio
    async def test_rate_limit_is_10_a_minute_per_user(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        codes = [http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={}).status_code for _ in range(11)]
        assert codes == [200] * 10 + [429]
        other, _ = user_client(app, db, UserRole.ADMIN)  # another user has their own allowance
        assert other.post(f"/api/drafts/{drafts[1].id}/regenerate", json={}).status_code == 200

    @pytest.mark.asyncio
    async def test_never_stores_a_placeholder(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        llm = use_llm(
            monkeypatch,
            DraftedMessage(subject="Hello", message=BAD),
            DraftedMessage(subject="Fresh", message=NEW),
        )
        assert http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={}).status_code == 200
        assert len(llm.prompts) == 2 and "PREVIOUS DRAFT WAS REJECTED" in llm.prompts[1]
        db.refresh(drafts[0])
        for bad in ("[", "]", "{{", "Your Name"):
            assert bad not in drafts[0].content

        use_llm(monkeypatch, DraftedMessage(subject="Hello", message=BAD))
        before = drafts[1].content
        response = http.post(f"/api/drafts/{drafts[1].id}/regenerate", json={})
        assert response.status_code == 422 and "kept" in response.json()["detail"]
        db.refresh(drafts[1])
        assert drafts[1].content == before and drafts[1].regenerated_at is None  # unchanged, no version
        assert db.query(DraftVersion).filter_by(draft_id=drafts[1].id).count() == 0

    @pytest.mark.asyncio
    async def test_all_providers_failing_is_a_clear_502_and_changes_nothing(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, LLMError("Groq HTTP 503; Gemini HTTP 429; Ollama unreachable"))
        response = http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={})
        assert response.status_code == 502 and "All AI providers failed" in response.json()["detail"]
        db.refresh(drafts[0])
        assert OLD in drafts[0].content and db.query(DraftVersion).count() == 0

    @pytest.mark.asyncio
    async def test_refuses_to_run_without_a_sender_name(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        monkeypatch.setattr(settings, "sender_name", " ")
        response = http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={})
        assert response.status_code == 503 and "SENDER_NAME" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_viewers_cannot_and_members_can(self, app, db, drafts, monkeypatch):
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        viewer, _ = user_client(app, db, UserRole.VIEWER)
        for path in (f"/api/drafts/{drafts[0].id}/regenerate", f"/api/drafts/{drafts[0].id}/restore", "/api/drafts/regenerate-all"):
            assert viewer.post(path, json={}).status_code == 403
        assert viewer.get(f"/api/drafts/{drafts[0].id}/versions").status_code == 200  # reading is allowed
        member, _ = user_client(app, db, UserRole.MEMBER)
        assert member.post(f"/api/drafts/{drafts[0].id}/regenerate", json={}).status_code == 200
        assert TestClient(app).post(f"/api/drafts/{drafts[0].id}/regenerate", headers=CSRF).status_code == 401


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_returns_the_previous_text_and_can_be_undone(self, app, db, drafts, monkeypatch):
        http, user = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        target = drafts[0]
        original = target.content
        http.post(f"/api/drafts/{target.id}/regenerate", json={})
        db.refresh(target)
        regenerated = target.content
        assert regenerated != original

        versions = http.get(f"/api/drafts/{target.id}/versions").json()
        assert [v["text"] for v in versions] == [original]

        restored = http.post(f"/api/drafts/{target.id}/restore", json={"version_id": versions[0]["id"]})
        assert restored.status_code == 200 and restored.json()["text"] == original
        db.refresh(target)
        assert target.content == original and target.subject == "Student partnership"
        assert target.review_status == ReviewStatus.PENDING_REVIEW and target.reviewed_by == user.id
        # what it replaced is now a version too, so the restore itself can be undone
        assert [v["text"] for v in http.get(f"/api/drafts/{target.id}/versions").json()] == [regenerated, original]
        http.post(f"/api/drafts/{target.id}/restore", json={})  # default: the latest version
        db.refresh(target)
        assert target.content == regenerated

    @pytest.mark.asyncio
    async def test_restore_with_no_version_is_404_and_sent_is_409(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        assert http.post(f"/api/drafts/{drafts[0].id}/restore", json={}).status_code == 404
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={})
        drafts[0].status = MessageStatus.SENT
        db.commit()
        assert http.post(f"/api/drafts/{drafts[0].id}/restore", json={}).status_code == 409
        db.refresh(drafts[0])
        assert NEW in drafts[0].content

    @pytest.mark.asyncio
    async def test_restore_restarts_a_running_hold(self, app, db, drafts, monkeypatch, notify):
        http, _ = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={})
        drafts[0].status, drafts[0].hold_until = MessageStatus.APPROVED, utc_now() + timedelta(minutes=1)
        db.commit()
        old_hold = drafts[0].hold_until
        http.post(f"/api/drafts/{drafts[0].id}/restore", json={})
        db.refresh(drafts[0])
        assert drafts[0].hold_until > old_hold
        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_drafts_list_reports_versions(self, app, db, drafts, monkeypatch):
        http, _ = user_client(app, db, UserRole.MEMBER)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        http.post(f"/api/drafts/{drafts[0].id}/regenerate", json={})
        rows = {r["id"]: r for r in http.get("/api/dashboard/drafts?view=awaiting").json()["items"]}
        assert rows[drafts[0].id]["has_previous"] is True and rows[drafts[0].id]["regenerated_at"]
        assert rows[drafts[1].id]["has_previous"] is False and rows[drafts[1].id]["regenerated_at"] is None


class TestRegenerateAll:
    @pytest.mark.asyncio
    async def test_rewrites_every_awaiting_draft_one_at_a_time_with_progress(self, app, db, drafts, monkeypatch):
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        drafts[2].status = MessageStatus.SENT  # not awaiting approval: skipped
        db.commit()
        with TestClient(app, headers=CSRF) as http:  # keeps the event loop alive for the background task
            member, _ = user_client(app, db, UserRole.MEMBER)
            http.cookies.set(security.SESSION_COOKIE, member.cookies.get(security.SESSION_COOKIE))
            started = http.post("/api/drafts/regenerate-all")
            assert started.status_code == 202 and started.json()["total"] == 2
            job_id = started.json()["job_id"]
            for _ in range(100):
                progress = http.get(f"/api/drafts/regenerate-jobs/{job_id}").json()
                if progress["status"] == "finished":
                    break
                time.sleep(0.05)
        assert progress["done"] == 2 and progress["failed"] == 0 and progress["total"] == 2
        db.expire_all()
        assert NEW in drafts[0].content and NEW in drafts[1].content
        assert OLD in drafts[2].content  # the sent one was not touched
        assert db.query(DraftVersion).count() == 2

    @pytest.mark.asyncio
    async def test_one_failure_does_not_stop_the_rest_and_is_reported(self, db, drafts, monkeypatch):
        use_llm(
            monkeypatch,
            DraftedMessage(subject="Fresh", message=NEW),
            LLMError("Groq HTTP 503"),
            DraftedMessage(subject="Fresh", message=NEW),
        )
        job = drafts_api.RegenerateJob(id="j", total=3, started_by=0)
        await drafts_api.run_regenerate_all(job, [m.id for m in drafts])
        assert (job.done, job.failed, job.status) == (2, 1, "finished")
        assert job.errors[0]["id"] == drafts[1].id and "All AI providers failed" in job.errors[0]["reason"]

    @pytest.mark.asyncio
    async def test_only_one_run_at_a_time_and_unknown_job_is_404(self, app, db, drafts):
        http, _ = user_client(app, db, UserRole.MEMBER)
        drafts_api.JOBS["busy"] = drafts_api.RegenerateJob(id="busy", total=5, started_by=1, done=2)
        assert http.post("/api/drafts/regenerate-all").status_code == 409
        assert http.get("/api/drafts/regenerate-jobs/nope").status_code == 404

    @pytest.mark.asyncio
    async def test_nothing_awaiting_finishes_at_once(self, app, db):
        http, _ = user_client(app, db, UserRole.MEMBER)
        body = http.post("/api/drafts/regenerate-all").json()
        assert body["total"] == 0 and body["status"] == "finished"


class TestForceRedraft:
    @pytest.mark.asyncio
    async def test_cli_regenerates_the_three_demo_drafts(self, db, drafts, monkeypatch, capsys):
        async def in_session(work):
            return await work(db)

        monkeypatch.setattr(scheduler, "in_session", in_session)
        monkeypatch.setattr(scheduler.JobMonitor, "apply_overrides", lambda self: None)
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        assert await cli.run_job("draft", force=True) == 0
        assert "OK draft: discarded=3, drafted=3, refused=0" in capsys.readouterr().out

        messages = list(db.scalars(select(Message)))
        assert len(messages) == 3  # the old drafts were discarded, not kept next to the new ones
        assert all(NEW in m.content and OLD not in m.content and m.status == MessageStatus.DRAFTED for m in messages)
        assert {o.state for o in db.scalars(select(Outreach))} == {OutreachState.DRAFTED}
        assert db.query(Outreach).count() == 3  # no duplicates

    def test_argument_parsing(self, monkeypatch, capsys):
        body = AsyncMock(return_value=0)
        monkeypatch.setattr(cli, "run_job", body)
        assert cli.main(["run-job", "draft", "--force", "--outreach-id", "4", "--outreach-id", "5"]) == 0
        body.assert_awaited_once_with("draft", True, [4, 5])

    @pytest.mark.asyncio
    async def test_force_only_applies_to_the_draft_job(self, capsys):
        assert await cli.run_job("replies", force=True) == 2
        assert await cli.run_job("draft", outreach_ids=[1]) == 2
        assert "only apply to the draft job" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_a_lead_with_a_sent_message_is_never_touched(self, db, drafts, monkeypatch):
        llm = use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        sent = drafts[0]
        sent.status = MessageStatus.SENT
        db.get(Outreach, sent.outreach_id).state = OutreachState.CONTACTED
        db.commit()
        sent_text, sent_id = sent.content, sent.id

        result = await drafter.force_redraft(db)
        assert (result.discarded, result.drafted) == (2, 2)
        assert [reason for _, reason in result.refused] == ["the lead has moved past DRAFTED"]
        db.refresh(sent)
        assert sent.id == sent_id and sent.status == MessageStatus.SENT and sent.content == sent_text
        assert db.get(Outreach, sent.outreach_id).state == OutreachState.CONTACTED
        assert len(llm.prompts) == 2  # the sent lead was not even redrafted

    @pytest.mark.asyncio
    async def test_a_sent_message_on_a_drafted_outreach_is_refused_by_id(self, db, drafts, monkeypatch):
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        outreach_id = drafts[0].outreach_id
        db.add(
            Message(outreach_id=outreach_id, direction=MessageDirection.OUTBOUND, content="already out", status=MessageStatus.SENT)
        )
        db.commit()
        result = await drafter.force_redraft(db, [outreach_id])
        assert result.drafted == 0 and result.discarded == 0
        assert result.refused == [(outreach_id, "a message was already sent or received")]
        assert db.query(Message).filter_by(outreach_id=outreach_id).count() == 2  # both kept

    @pytest.mark.asyncio
    async def test_explicit_ids_only_touch_those_leads_and_unknown_ids_are_reported(self, db, drafts, monkeypatch):
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        result = await drafter.force_redraft(db, [drafts[1].outreach_id, 9999])
        assert (result.discarded, result.drafted) == (1, 1) and result.refused == [(9999, "no such outreach")]
        db.expire_all()
        contents = {m.outreach_id: m.content for m in db.scalars(select(Message))}
        assert NEW in contents[drafts[1].outreach_id]
        assert OLD in contents[drafts[0].outreach_id] and OLD in contents[drafts[2].outreach_id]

    @pytest.mark.asyncio
    async def test_non_demo_leads_are_not_touched_without_an_id(self, db, drafts, monkeypatch):
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        company = repository.create_company(db, seed_demo.CompanyCreate(name="Real Corp", website=None))
        contact = repository.create_contact(
            db, seed_demo.ContactCreate(company_id=company.id, name="Real Person", email="real@realcorp.test", title="HR")
        )
        post = repository.create_post(
            db, seed_demo.PostCreate(url="https://www.linkedin.com/posts/real-person_hiring-1", company_id=company.id, poster_name="R")
        )
        real = repository.create_outreach(
            db,
            seed_demo.OutreachCreate(contact_id=contact.id, post_id=post.id, channel=seed_demo.ChannelType.EMAIL, state=OutreachState.DRAFTED),
        )
        db.add(Message(outreach_id=real.id, direction=MessageDirection.OUTBOUND, content="real draft", status=MessageStatus.DRAFTED))
        db.commit()
        await drafter.force_redraft(db)
        assert db.query(Message).filter_by(outreach_id=real.id).one().content == "real draft"

    @pytest.mark.asyncio
    async def test_open_attention_items_of_discarded_drafts_are_dismissed(self, db, drafts, monkeypatch):
        use_llm(monkeypatch, DraftedMessage(subject="Fresh", message=NEW))
        item = AttentionItem(type="needs_manual", payload={"message_id": drafts[0].id, "reason": "x"})
        db.add(item)
        db.commit()
        await drafter.force_redraft(db)
        db.refresh(item)
        assert item.status == AttentionStatus.DISMISSED

    @pytest.mark.asyncio
    async def test_the_drafter_will_not_start_without_a_sender_name(self, db, monkeypatch):
        monkeypatch.setattr(settings, "sender_name", "")
        with pytest.raises(drafter.DrafterConfigError, match="SENDER_NAME"):
            Drafter(db=db)
        with pytest.raises(drafter.DrafterConfigError, match="SENDER_NAME"):
            await drafter.force_redraft(db)


class TestSeedReset:
    @pytest.fixture
    def seeded(self, db):
        return seed_demo.seed_leads(db, {ME})

    def test_reset_deletes_only_the_demo_leads_and_their_unsent_messages(self, db, seeded):
        company = repository.create_company(db, seed_demo.CompanyCreate(name="Real Corp", website=None))
        contact = repository.create_contact(
            db, seed_demo.ContactCreate(company_id=company.id, name="Real Person", email="real@realcorp.test", title="HR")
        )
        real_post = repository.create_post(
            db, seed_demo.PostCreate(url="https://www.linkedin.com/posts/real-person_hiring-1", company_id=company.id, poster_name="R")
        )
        real = repository.create_outreach(
            db,
            seed_demo.OutreachCreate(contact_id=contact.id, post_id=real_post.id, channel=seed_demo.ChannelType.EMAIL, state=OutreachState.DRAFTED),
        )
        db.add(Message(outreach_id=real.id, direction=MessageDirection.OUTBOUND, content="real draft", status=MessageStatus.DRAFTED))
        for outreach, _ in seeded:
            db.add(Message(outreach_id=outreach.id, direction=MessageDirection.OUTBOUND, content="demo draft", status=MessageStatus.DRAFTED))
        db.commit()

        deleted, skipped = seed_demo.reset_leads(db)
        assert (deleted, skipped) == (3, [])
        assert [o.id for o in db.scalars(select(Outreach))] == [real.id]
        assert [m.content for m in db.scalars(select(Message))] == ["real draft"]
        assert [c.name for c in db.scalars(select(Company))] == ["Real Corp"]
        assert [c.name for c in db.scalars(select(Contact))] == ["Real Person"]
        assert [p.url for p in db.scalars(select(Post))] == [real_post.url]

        again = seed_demo.seed_leads(db, {ME})  # recreated cleanly
        assert len(again) == 3 and db.query(Outreach).count() == 4

    def test_a_demo_lead_with_a_sent_message_is_kept(self, db, seeded):
        sent_outreach = seeded[0][0]
        db.add(Message(outreach_id=sent_outreach.id, direction=MessageDirection.OUTBOUND, content="out", status=MessageStatus.SENT))
        db.commit()
        deleted, skipped = seed_demo.reset_leads(db)
        assert deleted == 2 and len(skipped) == 1 and "already sent" in skipped[0]
        assert db.get(Outreach, sent_outreach.id) is not None
        assert db.query(Message).filter_by(status=MessageStatus.SENT).count() == 1

    def test_main_reset_flag(self, db, monkeypatch, capsys):
        monkeypatch.setattr(settings, "dry_run", True)
        seeded = seed_demo.seed_leads(db, {ME})
        db.add(Message(outreach_id=seeded[0][0].id, direction=MessageDirection.OUTBOUND, content="d", status=MessageStatus.DRAFTED))
        db.commit()
        import app.db as app_db

        monkeypatch.setattr(app_db, "SessionLocal", lambda: db)
        monkeypatch.setattr(db, "close", lambda: None)
        seed_demo.main(["--reset"])
        out = capsys.readouterr().out
        assert "Reset: deleted 3 demo lead(s)." in out and "Seeded 3 demo leads" in out
        assert db.query(Message).count() == 0
