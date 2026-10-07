"""Draft quality: roles in the prompt, no placeholders, signature from config, regenerate once, then fail."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import repository
from app.db.models import AttentionItem, AttentionType, ChannelType, Message, MessageStatus, OutreachState
from app.llm.prompts import draft_message_prompt
from app.workers import drafter
from app.workers.drafter import DraftedMessage, Drafter, build_context, compose, validate_draft

_SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_quality", _SEED)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

CLEAN = (
    "I saw your hiring push and wanted to introduce Universal AI University's B.Tech AI/ML students. "
    "We would value a short call this week to learn what your team needs and whether a structured "
    "partnership through an MoU could help both sides. Would you be open to a 15 minute conversation?"
)
BAD_BODY = "I am [Your Name] from AI ConnectX and would love to talk."


class FakeLLM:
    def __init__(self, *replies: DraftedMessage):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete_json(self, prompt, schema):
        self.prompts.append(prompt)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.fixture
def sender(monkeypatch):
    monkeypatch.setattr(settings, "sender_name", "Asha Verma")
    monkeypatch.setattr(settings, "sender_title", "Outreach Coordinator")
    monkeypatch.setattr(settings, "sender_contact", "asha@connectx.test")


@pytest.fixture
def leads(db, monkeypatch):
    monkeypatch.setattr(settings, "send_allowlist", "me@test.com")
    return seed_demo.seed_leads(db, {"me@test.com"})


def use_llm(monkeypatch, *replies):
    llm = FakeLLM(*replies)
    monkeypatch.setattr(drafter, "llm_client", llm)
    return llm


def section(prompt: str, start: str, end: str) -> str:
    return prompt.split(start, 1)[1].split(end, 1)[0]


class TestRolesInThePrompt:
    @pytest.mark.asyncio
    async def test_sender_and_recipient_are_correct_for_the_three_demo_leads(self, db, leads, monkeypatch):
        llm = use_llm(monkeypatch, DraftedMessage(subject="Student partnership", message=CLEAN))
        assert await Drafter(db=db).run_once() == 3
        assert len(llm.prompts) == 3
        for (outreach, _), item, prompt in zip(leads, seed_demo.DEMO_LEADS, llm.prompts):
            sender = section(prompt, "SENDER", "RECIPIENT")
            recipient = section(prompt, "RECIPIENT", "FACTS")
            assert "AI ConnectX team" in sender and "Universal AI University" in sender
            assert item["title"] not in sender and item["company"]["name"] not in sender
            assert item["name"] in recipient and item["title"] in recipient and item["company"]["name"] in recipient
            assert item["roles"][0] in prompt  # the lead has a post, so it is referenced
            assert "Refer to this hiring post" in prompt
            assert "Universal AI University" in section(prompt, "FACTS", "RULES")  # the facts file is included
            assert "no placeholders" in prompt.lower() and "subject" in prompt and "8 words" in prompt

    def test_no_post_means_no_reference(self):
        context = build_context(None, None)
        prompt = draft_message_prompt("email", {"name": "A", "title": "HR", "company": "Acme"}, context)
        assert "no hiring post for this lead" in prompt
        assert "Refer to this hiring post" not in prompt

    def test_rules_forbid_invented_claims_and_the_sender_writing_a_signature(self):
        prompt = draft_message_prompt("email", {"name": "A"}, {"post_context": "ML"}, facts="- A fact.").lower()
        for text in ("only facts from the facts", "premier", "50 to 90 words", "no signature", "one clear ask"):
            assert text in prompt


class TestCleanDrafts:
    @pytest.mark.asyncio
    async def test_no_placeholders_signature_and_subject_stored(self, db, leads, sender, monkeypatch):
        use_llm(monkeypatch, DraftedMessage(subject="One two three four five six seven eight nine ten", message=CLEAN))
        await Drafter(db=db).run_once()
        messages = list(db.scalars(select(Message)))
        assert len(messages) == 3
        for message, item in zip(messages, seed_demo.DEMO_LEADS):
            assert message.status == MessageStatus.DRAFTED
            for bad in ("[", "]", "{{", "Your Name"):
                assert bad not in message.content
            first = item["name"].split()[0]
            assert message.content.startswith(f"Hi {first},\n\n")
            assert message.content.endswith(
                "Regards,\nAsha Verma\nOutreach Coordinator, AI ConnectX\nasha@connectx.test"
            )
            assert message.subject == "One two three four five six seven eight"  # trimmed to 8 words

    def test_signature_without_a_sender_name_is_the_team(self, monkeypatch):
        monkeypatch.setattr(settings, "sender_name", "")
        assert compose("Priya Nair", "Body.") == "Hi Priya,\n\nBody.\n\nRegards,\nThe AI ConnectX team"

    def test_signature_uses_org_setting(self, monkeypatch):
        monkeypatch.setattr(settings, "sender_name", "")
        monkeypatch.setattr(settings, "org_name", "ConnectX Labs")
        assert compose(None, "Body.").endswith("Regards,\nThe ConnectX Labs team")
        assert compose(None, "Body.").startswith("Hi there,")


class TestValidation:
    def test_placeholders_are_rejected(self):
        info = {"name": "A", "title": "HR", "company": "Acme"}
        for body in ("Hi [Name]", "x ]", "Hello {{name}}", "Regards, Your Name", "regards, your name"):
            assert validate_draft("Subject", body, info), body
        assert validate_draft("Subject [x]", "fine", info)
        assert validate_draft("Subject", CLEAN, info) == []

    def test_sender_claiming_the_recipients_title_or_company_is_rejected(self):
        info = {"name": "Priya", "title": "Senior Technical Recruiter", "company": "Northwind AI"}
        pad = " " + " ".join(["more"] * 60)  # long enough to pass the word-count rule, so only the claim is tested
        assert validate_draft("S", "I am a Senior Technical Recruiter and reaching out." + pad, info)
        assert validate_draft("S", "I'm part of the team at Northwind AI." + pad, info)
        assert validate_draft("S", "We are hiring at Northwind AI too." + pad, info)
        assert validate_draft("S", "As a Senior Technical Recruiter at Northwind AI, you see many CVs." + pad, info) == []

    def test_body_length_outside_50_to_90_words_is_rejected_with_tolerance(self):
        info = {"name": "A", "title": "HR", "company": "Acme"}
        words = lambda n: " ".join(["word"] * n)  # noqa: E731
        email = ChannelType.EMAIL
        assert validate_draft("S", words(50), info, email) == [] and validate_draft("S", words(90), info, email) == []
        assert validate_draft("S", words(40), info, email) == [] and validate_draft("S", words(108), info, email) == []
        assert any("words" in p for p in validate_draft("S", words(39), info, email))
        assert any("words" in p for p in validate_draft("S", words(109), info, email))
        # LinkedIn notes are capped at 200 characters, so the length rule does not apply to them
        assert validate_draft("S", words(5), info, ChannelType.LINKEDIN) == []

    def test_sender_title_equal_to_recipient_title_is_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "sender_title", "senior technical recruiter")
        info = {"name": "Priya", "title": "Senior Technical Recruiter", "company": "Northwind AI"}
        assert "the sender's title equals the recipient's title" in validate_draft("S", CLEAN, info)


class TestRegenerateThenFail:
    @pytest.mark.asyncio
    async def test_regenerates_once_with_the_reason(self, db, leads, monkeypatch):
        llm = use_llm(
            monkeypatch,
            DraftedMessage(subject="Hello", message=BAD_BODY),
            DraftedMessage(subject="Student partnership", message=CLEAN),
        )
        await Drafter(db=db).draft_outreach(leads[0][0])
        assert len(llm.prompts) == 2
        assert "PREVIOUS DRAFT WAS REJECTED" in llm.prompts[1] and "placeholder" in llm.prompts[1]
        message = db.scalars(select(Message)).one()
        assert message.status == MessageStatus.DRAFTED and "Your Name" not in message.content

    @pytest.mark.asyncio
    async def test_second_failure_marks_failed_and_creates_attention_item(self, db, leads, monkeypatch):
        llm = use_llm(monkeypatch, DraftedMessage(subject="Hello", message=BAD_BODY))
        outreach = leads[0][0]
        assert await Drafter(db=db).draft_outreach(outreach) == 1
        assert len(llm.prompts) == 2  # exactly one regeneration
        message = db.scalars(select(Message)).one()
        assert message.status == MessageStatus.FAILED  # never sendable: the queue sends APPROVED only
        item = db.scalars(select(AttentionItem)).one()
        assert item.type == AttentionType.NEEDS_MANUAL
        assert item.payload["message_id"] == message.id and item.payload["outreach_id"] == outreach.id
        assert "placeholder" in item.payload["reason"]
        # Out of ENRICHED, so the next run does not draft the same lead again.
        assert repository.get_outreach(db, outreach.id).state == OutreachState.DRAFTED
        assert await Drafter(db=db).run_once() == 2  # only the other two demo leads
        assert db.query(AttentionItem).count() == 3  # their drafts fail the same way


class TestFactsFile:
    def test_comments_are_stripped_and_university_is_named(self):
        facts = drafter.load_facts()
        assert "<!--" not in facts and "Universal AI University" in facts
        assert not any(marker in facts for marker in ("[", "]", "{{"))

    def test_missing_file_means_no_facts(self, tmp_path):
        assert drafter.load_facts(tmp_path / "nope.md") == ""
        prompt = draft_message_prompt("email", {"name": "A"}, {}, facts="")
        assert "make no claims" in prompt


class TestStoredSubject:
    @pytest.mark.asyncio
    async def test_queue_sends_the_stored_subject_and_falls_back_without_one(self):
        from tests.test_send_queue import simple_setup

        h, messages, _, _ = simple_setup(2)
        messages[0].subject = "Student partnership"
        await h.run()
        subjects = [call.args[1] for call in h.client.send_email.await_args_list]
        assert subjects[0] == "Student partnership"
        assert subjects[1].startswith("AI/ML talent partnership")
