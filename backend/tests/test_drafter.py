from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import drafts
from app.auth.deps import get_current_user
from app.db import get_session
from app.db.models import ChannelType, MessageStatus, OutreachState
from app.workers import drafter
from app.workers.drafter import Drafter, build_context, pick_channels


def make_contact(linkedin: str | None, email: str | None, unsub: bool = False):
    return SimpleNamespace(
        id=1,
        company_id=1,
        name="Jane",
        title="HR",
        linkedin_url=linkedin,
        email=email,
        is_unsubscribed=unsub,
    )


class TestPickChannels:
    def test_linkedin_preferred(self):
        c = make_contact("https://li/in/j", "j@x.com")
        assert pick_channels(c, False) == [ChannelType.LINKEDIN]

    def test_email_fallback(self):
        assert pick_channels(make_contact(None, "j@x.com"), False) == [ChannelType.EMAIL]

    def test_post_triggered_both(self):
        c = make_contact("https://li/in/j", "j@x.com")
        assert pick_channels(c, True) == [ChannelType.LINKEDIN, ChannelType.EMAIL]

    def test_post_triggered_single_available(self):
        assert pick_channels(make_contact(None, "j@x.com"), True) == [ChannelType.EMAIL]

    def test_none_available(self):
        assert pick_channels(make_contact(None, None), True) == []


def test_build_context_defaults():
    assert build_context(None, None) == {
        "post_context": "General outreach",
        "company_size": "Unknown",
        "has_post": "no",
    }


def test_build_context_uses_roles():
    post = SimpleNamespace(roles=["ML Intern", "SDE"], raw_text="x")
    assert build_context(post, "startup")["post_context"] == "ML Intern, SDE"


@pytest.fixture
def outreach():
    return SimpleNamespace(id=10, contact_id=1, post_id=5, state=OutreachState.ENRICHED)


class TestDrafter:
    @pytest.mark.asyncio
    async def test_post_triggered_drafts_both_channels(self, outreach):
        contact = make_contact("https://li/in/j", "j@x.com")
        company = SimpleNamespace(name="Acme", size="startup")
        post = SimpleNamespace(roles=["ML"], raw_text="hi")
        repo = drafter.repository
        with patch.object(repo, "get_contact", return_value=contact), patch.object(
            repo, "get_company", return_value=company
        ), patch.object(repo, "get_post", return_value=post), patch.object(
            repo, "update_outreach", side_effect=lambda db, o, d: o
        ) as upd, patch.object(
            repo, "create_outreach", return_value=SimpleNamespace(id=11)
        ) as create_o, patch.object(
            repo, "create_message"
        ) as create_m, patch.object(
            drafter, "draft_message", AsyncMock(return_value=drafter.Draft("Talent partnership", "hello"))
        ):
            count = await Drafter(db=MagicMock()).draft_outreach(outreach)
        assert count == 2
        assert create_o.call_count == 1
        assert create_m.call_count == 2
        for call in create_m.call_args_list:
            assert call.args[1].status == MessageStatus.DRAFTED
            assert call.args[1].content.startswith("Hi Jane,\n\nhello\n\nRegards,")
        subjects = {c.args[1].subject for c in create_m.call_args_list}
        assert subjects == {None, "Talent partnership"}  # only the email draft keeps a subject
        states = [c.args[2].state for c in upd.call_args_list if c.args[2].state]
        assert states == [OutreachState.DRAFTED, OutreachState.DRAFTED]

    @pytest.mark.asyncio
    async def test_unsubscribed_skipped(self, outreach):
        contact = make_contact("https://li/in/j", None, unsub=True)
        with patch.object(drafter.repository, "get_contact", return_value=contact), patch.object(
            drafter.repository, "create_message"
        ) as create_m:
            assert await Drafter(db=MagicMock()).draft_outreach(outreach) == 0
        create_m.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_channel_skipped(self, outreach):
        with patch.object(
            drafter.repository, "get_contact", return_value=make_contact(None, None)
        ), patch.object(drafter.repository, "create_message") as create_m:
            assert await Drafter(db=MagicMock()).draft_outreach(outreach) == 0
        create_m.assert_not_called()

    @pytest.mark.asyncio
    async def test_draft_message_calls_llm(self):
        llm = MagicMock()
        body = " ".join(["word"] * 60)
        llm.complete_json = AsyncMock(return_value=drafter.DraftedMessage(message=f" {body} "))
        with patch.object(drafter, "llm_client", llm):
            out = await drafter.draft_message(ChannelType.EMAIL, {"name": "J"}, {})
        assert out == drafter.Draft(subject="", body=body)


class TestDraftsApi:
    def make_client(self, message):
        app = FastAPI()
        app.include_router(drafts.router)
        app.dependency_overrides[get_session] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=7, role="member")
        outreach = SimpleNamespace(channel=ChannelType.EMAIL)

        def update(_db, msg, data):
            for key, value in data.model_dump(exclude_unset=True).items():
                setattr(msg, key, value)
            return msg

        self.patches = [
            patch.object(drafts.repository, "get_message", return_value=message),
            patch.object(drafts.repository, "get_outreach", return_value=outreach),
            patch.object(drafts.repository, "update_message", side_effect=update),
        ]
        for p in self.patches:
            p.start()
        return TestClient(app)

    def teardown_method(self):
        for p in getattr(self, "patches", []):
            p.stop()

    def msg(self, status=MessageStatus.DRAFTED):
        return SimpleNamespace(id=1, outreach_id=2, content="old", status=status)

    def test_approve_does_not_send(self):
        client = self.make_client(self.msg())
        r = client.post("/drafts/1/approve")
        assert r.status_code == 200
        assert r.json()["status"] == "approved"
        assert r.json()["dry_run"] is True

    def test_reject(self):
        client = self.make_client(self.msg())
        assert client.post("/drafts/1/reject").json()["status"] == "rejected"

    def test_edit_text(self):
        client = self.make_client(self.msg())
        r = client.patch("/drafts/1", json={"content": "new text"})
        assert r.json()["content"] == "new text"
        assert r.json()["status"] == "drafted"

    def test_edit_empty_rejected(self):
        client = self.make_client(self.msg())
        assert client.patch("/drafts/1", json={"content": ""}).status_code == 422

    def test_approve_twice_conflicts(self):
        client = self.make_client(self.msg(MessageStatus.APPROVED))
        assert client.post("/drafts/1/approve").status_code == 409
        assert client.patch("/drafts/1", json={"content": "x"}).status_code == 409

    def test_missing_draft_404(self):
        client = self.make_client(None)
        assert client.post("/drafts/1/approve").status_code == 404
