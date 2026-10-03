"""End-to-end proof that the pipeline works in dry-run before anything real is sent.

Real code, real (SQLite) database, three seeded demo posts. Only the outside world is
faked: the LLM, people search, email verification, LinkedIn, Outlook and WhatsApp. The
network guard in conftest.py fails any test that tries to reach a real service.
"""

from __future__ import annotations

import importlib.util
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.api import dashboard
from app.api import whatsapp_webhook as wh
from app.channels.base import IncomingReply, Thread
from app.config import settings
from app.db import SessionLocal, engine
from app.db.models import (
    AttentionItem,
    AttentionType,
    ChannelType,
    Company,
    Contact,
    ContactRole,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    Post,
    PostStatus,
    ReviewStatus,
)
from app.llm.client import llm_client
from app.llm.prompts import CompanyClassificationResult, ReplyClassificationResult
from app.workers.drafter import Drafter, DraftedMessage
from app.workers.enrichment import EnrichmentWorker
from app.workers.pipeline import Pipeline
from app.workers.replies import ReplyWorker
from app.workers.reports import SendReporter
from app.workers.review import ReviewCommands, ReviewService
from app.workers.send_queue import IST, SendQueue
from app.workers.targeting import TargetingWorker

LI = ChannelType.LINKEDIN
EM = ChannelType.EMAIL

_SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo", _SEED)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

PREVIEW = re.compile(
    r'^#(\d+) (LinkedIn|Email) to (.+) \((.+)\)\n"(.*)"\nSends in (\d+) min\.\n'
    r"Reply: SEND \1 \| CANCEL \1 \| EDIT \1 <new text>$",
    re.DOTALL,
)


# ─── Fakes for everything outside the process ──────────────


class Clock:
    def __init__(self, at: datetime):
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)

    def set_ist(self, year: int, month: int, day: int, hour: int, minute: int = 0) -> None:
        self.now = datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(timezone.utc)


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.reply_intent = "interested"

    async def complete_json(self, prompt: str, schema: type):
        self.calls.append(schema.__name__)
        if schema is DraftedMessage:
            name = re.search(r"- Name: (.+)", prompt).group(1)
            return DraftedMessage(message=f"Hi {name.split()[0]}, AI ConnectX connects companies with AI/ML students. Open to a call?")
        if schema is CompanyClassificationResult:
            return CompanyClassificationResult(company_type="mid_tier", reasoning="test")
        if schema is ReplyClassificationResult:
            return ReplyClassificationResult(intent=self.reply_intent)
        raise AssertionError(f"Unexpected LLM request for {schema.__name__}")


class FakeLinkedIn:
    """Stands in for the Playwright client; keeps the shared daily-cap contract."""

    available = True

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, str, str]] = []
        self.actions: dict[str, int] = {}  # per IST day, like the real client
        self.connected = True
        self.incoming: list[IncomingReply] = []

    def _today(self) -> str:
        return self.clock().astimezone(IST).date().isoformat()

    def remaining_today(self) -> int:
        return max(0, settings.linkedin_daily_cap - self.actions.get(self._today(), 0))

    def _record(self, kind: str, target: str, text: str) -> None:
        self.calls.append((kind, target, text))
        self.actions[self._today()] = self.actions.get(self._today(), 0) + 1

    async def send_connection_note(self, profile_url: str, note: str) -> None:
        assert len(note) <= 200
        self._record("connect", profile_url, note)

    async def send_dm(self, profile_url: str, text: str) -> str:
        self._record("dm", profile_url, text)
        return profile_url.rstrip("/").lower()

    async def reply(self, thread: Thread, text: str) -> str | None:
        self._record("reply", thread.recipient, text)
        return thread.thread_id

    async def is_connected(self, profile_url: str) -> bool:
        return self.connected

    async def read_new_replies(self, since: datetime) -> list[IncomingReply]:
        return list(self.incoming)

    def of(self, kind: str) -> list[tuple[str, str, str]]:
        return [call for call in self.calls if call[0] == kind]


class FakeOutlook:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str | None]] = []
        self.incoming: list[IncomingReply] = []

    async def send_email(self, to: str, subject: str, body: str, conversation_id: str | None = None) -> str:
        self.calls.append(("send", to, body, subject))
        return f"conv-{len(self.calls)}"

    async def reply(self, thread: Thread, text: str) -> str | None:
        self.calls.append(("reply", thread.recipient, text, thread.subject))
        return thread.thread_id or f"conv-{len(self.calls)}"

    async def read_new_replies(self, since: datetime) -> list[IncomingReply]:
        return list(self.incoming)

    def of(self, kind: str) -> list[tuple[str, str, str, str | None]]:
        return [call for call in self.calls if call[0] == kind]


class World:
    """The pipeline wired to a SQLite database, a controllable clock and fake externals."""

    def __init__(self, db, monkeypatch) -> None:
        self.db = db
        self.clock = Clock(datetime(2026, 10, 5, 10, 30, tzinfo=IST).astimezone(timezone.utc))  # Monday
        self.llm = FakeLLM()
        self.linkedin = FakeLinkedIn(self.clock)
        self.outlook = FakeOutlook()
        self.whatsapp: list[str] = []
        self.sleep = AsyncMock()
        self.review = ReviewService(db, notify=self.notify, clock=self.clock)
        self.reporter = SendReporter(db, notify=self.notify, clock=self.clock)
        self.commands = ReviewCommands(session_factory=lambda: self.db, notify=self.notify, clock=self.clock)

        monkeypatch.setattr(llm_client, "complete_json", self.llm.complete_json)

        async def find_linkedin_profile(_self, company_name, role):
            return "https://www.linkedin.com/in/maria-gomez"

        async def scrape_company_emails(_self, website):
            return []

        async def verify_email(_self, email):
            return 0.9 if re.match(r"^[a-z]+\.[a-z]+@", email) else 0.0

        monkeypatch.setattr(EnrichmentWorker, "_find_linkedin_profile", find_linkedin_profile)
        monkeypatch.setattr(EnrichmentWorker, "_scrape_company_emails", scrape_company_emails)
        monkeypatch.setattr(EnrichmentWorker, "_verify_email", verify_email)

    async def notify(self, text: str) -> str:
        self.whatsapp.append(text)
        return f"wamid.{len(self.whatsapp)}"

    # ─── Steps ─────────────────────────────────────────────

    def seed(self) -> list[Post]:
        return seed_demo.seed_posts(self.db)

    async def run_pipeline(self):
        self.seed()
        pipeline = Pipeline(db=self.db, targeting=TargetingWorker(db=self.db), drafter=Drafter(db=self.db))
        return await pipeline.run_once()

    def queue(self, dry_run: bool | None = None) -> SendQueue:
        return SendQueue(
            db=self.db, client=self.outlook, linkedin=self.linkedin, dry_run=dry_run, sleep=self.sleep,
            clock=self.clock, rng=random.Random(7), review=self.review, reporter=self.reporter,
        )

    def replies(self) -> ReplyWorker:
        return ReplyWorker(
            db=self.db, channels={LI: self.linkedin, EM: self.outlook}, llm=self.llm,
            notify=self.notify, clock=self.clock,
        )

    def approve_all(self) -> int:
        approved = 0
        for message in list(self.db.scalars(
            select(Message).where(Message.direction == MessageDirection.OUTBOUND, Message.status == MessageStatus.DRAFTED)
        )):
            dashboard.release_or_approve(self.db, message)
            approved += 1
        return approved

    async def hold_all(self):
        self.approve_all()
        return await self.queue().run_once()

    # ─── Lookups (always re-query: ReviewCommands closes the shared session) ──

    def message(self, person: str, channel: ChannelType, last: bool = False) -> Message:
        stmt = (
            select(Message)
            .join(Outreach, Outreach.id == Message.outreach_id)
            .join(Contact, Contact.id == Outreach.contact_id)
            .where(Contact.name == person, Outreach.channel == channel, Message.direction == MessageDirection.OUTBOUND)
            .order_by(Message.id.desc() if last else Message.id)
        )
        found = self.db.scalars(stmt).first()
        assert found is not None, f"no {channel.value} message for {person}"
        return found

    def outreach(self, person: str, channel: ChannelType) -> Outreach:
        stmt = (
            select(Outreach).join(Contact, Contact.id == Outreach.contact_id)
            .where(Contact.name == person, Outreach.channel == channel)
        )
        found = self.db.scalars(stmt).first()
        assert found is not None
        return found

    def previews(self) -> list[str]:
        return [text for text in self.whatsapp if text.startswith("#")]

    def sent_by_channel_day(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        rows = self.db.execute(
            select(Message, Outreach.channel)
            .join(Outreach, Outreach.id == Message.outreach_id)
            .where(Message.direction == MessageDirection.OUTBOUND, Message.status == MessageStatus.SENT)
        ).all()
        for message, channel in rows:
            key = (channel.value, message.sent_at.astimezone(IST).date().isoformat())
            counts[key] = counts.get(key, 0) + 1
        return counts


@pytest.fixture
def world(db, monkeypatch):
    return World(db, monkeypatch)


# ─── Isolation: the suite can never touch the real world ───


class TestIsolation:
    def test_settings_are_dummies_and_dry_run_is_forced(self):
        assert settings.dry_run is True
        assert settings.groq_api_key == "test-groq-key" and settings.whatsapp_token == "test-whatsapp-token"
        assert settings.google_client_secret == "test-google-client-secret" and settings.postgres_host == "postgres.invalid"
        assert settings.tpo_email.endswith(".invalid")

    def test_database_is_a_throwaway_sqlite(self, db):
        assert engine.dialect.name == "sqlite" and SessionLocal.kw["bind"] is engine
        db.add(Company(name="Scratch Co"))
        db.commit()
        assert db.scalar(select(Company.name)) == "Scratch Co"

    @pytest.mark.asyncio
    async def test_unmocked_http_call_is_blocked(self, network_guard):
        with pytest.raises(RuntimeError, match="Network call"):
            async with httpx.AsyncClient() as client:
                await client.get("https://api.groq.com/openai/v1/models")
        with pytest.raises(RuntimeError, match="Network call"):
            httpx.get("https://graph.microsoft.com/v1.0/me")
        assert len(network_guard.violations) == 2
        network_guard.clear()  # these violations were the point of the test

    def test_raw_sockets_and_dns_are_blocked(self, network_guard):
        import socket

        with pytest.raises(RuntimeError, match="Network call"):
            socket.create_connection(("example.com", 443), timeout=1)
        with pytest.raises(RuntimeError, match="Network call"):
            socket.getaddrinfo("graph.facebook.com", 443)
        network_guard.clear()

    def test_a_swallowed_network_error_still_fails_the_test(self, network_guard):
        """Code under test often catches Exception; the guard remembers the attempt anyway."""
        try:
            httpx.get("https://api.groq.com")
        except Exception:
            pass
        assert network_guard.violations
        network_guard.clear()


# ─── The pipeline ──────────────────────────────────────────


class TestSeedAndTargeting:
    def test_seed_is_idempotent_and_uses_fake_domains(self, db):
        first = seed_demo.seed_posts(db)
        second = seed_demo.seed_posts(db)
        assert [p.id for p in first] == [p.id for p in second] and len(first) == 3
        assert all(p.status == PostStatus.NEW and p.company_id for p in first)
        assert not any(".com" in (p.raw_text or "") for p in first)

    @pytest.mark.asyncio
    async def test_posts_become_targeted_enriched_and_drafted(self, world):
        report = await world.run_pipeline()
        db = world.db
        assert (report.targeted, report.skipped, report.failed, report.drafted) == (3, 0, 0, 5)
        assert all(p.status == PostStatus.PROCESSED for p in db.scalars(select(Post)))

        contacts = {c.name: c for c in db.scalars(select(Contact))}
        assert set(contacts) == {"Priya Nair", "Maria Gomez", "Sara Khan"}
        # recruiter poster: the poster is the contact, found no email (company has no website)
        priya = contacts["Priya Nair"]
        assert priya.role == ContactRole.RECRUITER and priya.linkedin_url.endswith("priya-nair-demo") and priya.email is None
        # employee poster: HR contact found through enrichment, with a verified email
        maria = contacts["Maria Gomez"]
        assert maria.role == ContactRole.HR and maria.email == "maria.gomez@globex-labs.test"
        assert "CompanyClassificationResult" in world.llm.calls
        assert db.scalar(select(Company.size).where(Company.name == "Globex Labs")) == "mid_tier"
        # email in the post: that address is used
        assert contacts["Sara Khan"].email == "careers@initech-robotics.test"

        outreaches = list(db.scalars(select(Outreach)))
        assert len(outreaches) == 5 and all(o.state == OutreachState.DRAFTED for o in outreaches)
        assert sorted((db.get(Contact, o.contact_id).name, o.channel.value) for o in outreaches) == [
            ("Maria Gomez", "email"), ("Maria Gomez", "linkedin"), ("Priya Nair", "linkedin"),
            ("Sara Khan", "email"), ("Sara Khan", "linkedin"),
        ]
        drafts = list(db.scalars(select(Message)))
        assert len(drafts) == 5 and all(m.status == MessageStatus.DRAFTED and m.direction == MessageDirection.OUTBOUND for m in drafts)
        assert all(m.content.startswith("Hi ") for m in drafts)
        assert world.linkedin.calls == [] and world.outlook.calls == [] and world.whatsapp == []

    @pytest.mark.asyncio
    async def test_nothing_is_sent_before_approval(self, world):
        await world.run_pipeline()
        report = await world.queue(dry_run=False).run_once()
        assert report.held == 0 and report.sent == 0
        assert world.linkedin.calls == [] and world.outlook.calls == []


# ─── Hold window and WhatsApp review ───────────────────────


class TestHoldAndReview:
    @pytest.mark.asyncio
    async def test_approved_drafts_are_held_with_a_whatsapp_preview(self, world):
        await world.run_pipeline()
        report = await world.hold_all()
        assert (report.held, report.sent) == (5, 0)

        previews = world.previews()
        assert len(previews) == 5
        for text in previews:
            match = PREVIEW.match(text)
            assert match, text
            assert match.group(6) == "10"
        assert any(p.startswith("#") and "LinkedIn to Priya Nair (Northwind AI)" in p for p in previews)

        expected_end = world.clock.now + timedelta(minutes=10)
        for message in world.db.scalars(select(Message)):
            assert message.hold_until == expected_end and message.review_status == ReviewStatus.PENDING_REVIEW
            assert message.whatsapp_message_id and message.status == MessageStatus.APPROVED
        assert world.linkedin.calls == [] and world.outlook.calls == []
        assert (await world.queue(dry_run=False).run_once()).waiting == 5  # still inside the hold

    @pytest.mark.asyncio
    async def test_cancel_over_whatsapp_means_never_sent(self, world):
        await world.run_pipeline()
        await world.hold_all()
        priya_id = world.message("Priya Nair", LI).id
        reply = await wh.dispatch(wh.parse_command(f"CANCEL {priya_id}"), world.commands)
        assert "cancelled" in reply
        priya = world.db.get(Message, priya_id)
        assert priya.review_status == ReviewStatus.CANCELLED and priya.status == MessageStatus.REJECTED

        world.clock.advance(minutes=11)
        await world.queue(dry_run=False).run_once()
        targets = [call[1] for call in world.linkedin.calls]
        assert all("priya-nair" not in target for target in targets) and len(targets) >= 1
        assert "already cancelled" in await wh.dispatch(wh.parse_command(f"SEND {priya_id}"), world.commands)

    @pytest.mark.asyncio
    async def test_edit_replaces_text_restarts_the_hold_and_previews_again(self, world):
        await world.run_pipeline()
        await world.hold_all()
        priya = world.message("Priya Nair", LI)
        first_hold = world.db.get(Message, priya.id).hold_until
        world.clock.advance(minutes=6)
        before = len(world.previews())

        reply = await wh.dispatch(wh.parse_command(f"EDIT {priya.id} Hello Priya, short and sweet."), world.commands)
        assert "Hold restarted" in reply
        priya = world.db.get(Message, priya.id)
        assert priya.edited_text == "Hello Priya, short and sweet." and priya.review_status == ReviewStatus.EDITED
        assert priya.hold_until == world.clock.now + timedelta(minutes=10) and priya.hold_until > first_hold
        new = world.previews()[before:]
        assert len(new) == 1 and '"Hello Priya, short and sweet."' in new[0] and f"#{priya.id} LinkedIn to Priya Nair" in new[0]

        queue = world.queue(dry_run=False)
        world.clock.advance(minutes=5)  # the original hold has passed, the restarted one has not
        await queue.run_once()
        assert not [c for c in world.linkedin.calls if "priya-nair" in c[1]]
        world.clock.advance(minutes=6)
        await queue.run_once()
        sent = [c for c in world.linkedin.calls if "priya-nair" in c[1]]
        assert sent and sent[0][2] == "Hello Priya, short and sweet."  # the edited text, not the draft

    @pytest.mark.asyncio
    async def test_dry_run_releases_holds_but_sends_nothing(self, world):
        assert settings.dry_run is True
        await world.run_pipeline()
        await world.hold_all()
        world.clock.advance(minutes=11)
        report = await world.queue().run_once()  # dry_run comes from the settings
        assert (report.dry_run, report.sent) == (5, 0)
        assert world.linkedin.calls == [] and world.outlook.calls == []
        for message in world.db.scalars(select(Message)):
            assert message.status == MessageStatus.APPROVED and message.review_status == ReviewStatus.AUTO_RELEASED
        assert all(c.last_touch_at is None for c in world.db.scalars(select(Contact)))


# ─── Guardrails ────────────────────────────────────────────


class TestGuardrails:
    @pytest.mark.asyncio
    async def test_daily_caps_hold_per_channel_and_reset_next_day(self, world, monkeypatch):
        monkeypatch.setattr(settings, "linkedin_daily_cap", 1)
        monkeypatch.setattr(settings, "email_daily_cap", 1)
        await world.run_pipeline()
        await world.hold_all()
        world.clock.advance(minutes=11)
        report = await world.queue(dry_run=False).run_once()
        assert report.stopped == "daily_cap"
        assert (len(world.linkedin.calls), len(world.outlook.calls)) == (1, 1)
        delays = [call.args[0] for call in world.sleep.await_args_list]
        assert delays and all(60 <= d <= 240 for d in delays)  # random 60-240s between sends

        world.clock.advance(minutes=5)  # still the same day: the caps stay used up
        await world.queue(dry_run=False).run_once()
        assert (len(world.linkedin.calls), len(world.outlook.calls)) == (1, 1)

        world.clock.advance(days=1)  # next IST morning
        await world.queue(dry_run=False).run_once()
        assert len(world.linkedin.calls) == 2 and len(world.outlook.calls) >= 1
        assert max(world.sent_by_channel_day().values()) == 1  # never more than the cap on any day

    @pytest.mark.asyncio
    async def test_one_touch_per_person_every_14_days(self, world):
        await world.run_pipeline()
        await world.hold_all()
        world.clock.advance(minutes=11)
        await world.queue(dry_run=False).run_once()
        sent_people = {"sara-khan": 0, "maria-gomez": 0}
        for kind, target, _ in world.linkedin.calls:
            for key in sent_people:
                sent_people[key] += key in target
        for _, to, _, _ in world.outlook.calls:
            sent_people["sara-khan"] += to.startswith("careers@")
            sent_people["maria-gomez"] += to.startswith("maria.gomez@")
        assert sent_people == {"sara-khan": 1, "maria-gomez": 1}  # LinkedIn or email, not both

    @pytest.mark.asyncio
    async def test_nothing_sends_outside_the_window(self, world):
        await world.run_pipeline()
        await world.hold_all()
        world.clock.set_ist(2026, 10, 5, 18, 0)  # holds have passed, window has closed
        report = await world.queue(dry_run=False).run_once()
        assert report.stopped == "outside_window" and report.sent == 0
        assert world.linkedin.calls == [] and world.outlook.calls == []
        world.clock.set_ist(2026, 10, 6, 10, 5)  # next morning
        report = await world.queue(dry_run=False).run_once()
        assert report.sent >= 1

    @pytest.mark.asyncio
    async def test_a_hold_that_ends_after_17_00_waits_until_10_00(self, world):
        await world.run_pipeline()
        world.clock.set_ist(2026, 10, 5, 16, 55)
        await world.hold_all()
        tomorrow = datetime(2026, 10, 6, 10, 0, tzinfo=IST).astimezone(timezone.utc)
        assert all(m.hold_until == tomorrow for m in world.db.scalars(select(Message)))
        assert all(PREVIEW.match(p).group(6) == "1025" for p in world.previews())
        world.clock.set_ist(2026, 10, 5, 16, 59)
        assert (await world.queue(dry_run=False).run_once()).sent == 0
        world.clock.set_ist(2026, 10, 6, 10, 0)
        assert (await world.queue(dry_run=False).run_once()).sent >= 1

    @pytest.mark.asyncio
    async def test_stop_over_whatsapp_pauses_everything_and_start_resumes(self, world):
        await world.run_pipeline()
        await world.hold_all()
        world.clock.advance(minutes=11)
        assert "Paused" in await wh.dispatch(wh.parse_command("STOP"), world.commands)
        report = await world.queue(dry_run=False).run_once()
        assert report.stopped == "paused" and world.linkedin.calls == [] and world.outlook.calls == []
        assert "Resumed" in await wh.dispatch(wh.parse_command("START"), world.commands)
        assert (await world.queue(dry_run=False).run_once()).sent >= 1

    @pytest.mark.asyncio
    async def test_do_not_contact_list_blocks_a_lead(self, world):
        from app.db.models import Setting

        await world.run_pipeline()
        world.db.add(Setting(key="do_not_contact", value="https://www.linkedin.com/in/priya-nair-demo"))
        world.db.commit()
        await world.hold_all()
        world.clock.advance(minutes=11)
        await world.queue(dry_run=False).run_once()
        assert not [c for c in world.linkedin.calls if "priya-nair" in c[1]]
        assert world.db.get(Message, world.message("Priya Nair", LI).id).status == MessageStatus.REJECTED


# ─── Replies ───────────────────────────────────────────────


class TestInterestedReply:
    async def get_to_dm_sent(self, world: World, person: str = "Sara Khan") -> None:
        """Connection note sent, request accepted, first DM sent."""
        await world.run_pipeline()
        await world.hold_all()
        world.clock.advance(minutes=11)
        await world.queue(dry_run=False).run_once()
        assert world.outreach(person, LI).state == OutreachState.CONNECT_SENT
        assert await world.replies().check_connections(world.linkedin) == 3
        assert world.outreach(person, LI).state == OutreachState.CONNECTED
        await world.queue(dry_run=False).run_once()  # starts the hold on the first DM
        world.clock.advance(minutes=11)
        await world.queue(dry_run=False).run_once()
        assert world.outreach(person, LI).state == OutreachState.DM_SENT
        assert len(world.linkedin.of("dm")) == 3

    @pytest.mark.asyncio
    async def test_interested_reply_triggers_tpo_dm_mou_email_and_attention_item(self, world):
        await self.get_to_dm_sent(world)
        db = world.db
        profile = "https://www.linkedin.com/in/sara-khan-demo"
        world.linkedin.incoming = [IncomingReply(
            message_id="li-r1", thread_id=profile, sender=profile,
            text="Yes, we are interested. Please share the details.", received_at=None,
        )]
        world.clock.advance(minutes=30)
        before = len(world.whatsapp)
        report = await world.replies().run_once(check_connections=False)
        assert report.handled == 1 and report.intents == {"interested": 1}

        assert world.outreach("Sara Khan", LI).state == OutreachState.INTERESTED
        assert world.outreach("Priya Nair", LI).state == OutreachState.DM_SENT  # others untouched
        inbound = db.scalars(select(Message).where(Message.direction == MessageDirection.INBOUND)).all()
        assert [m.content for m in inbound] == ["Yes, we are interested. Please share the details."]

        item = db.scalars(select(AttentionItem).where(AttentionItem.type == AttentionType.INTERESTED)).one()
        assert "Interested: Sara Khan (Initech Robotics) on LinkedIn" in item.payload["summary"]
        assert any(t.startswith("Interested: Sara Khan") for t in world.whatsapp[before:])

        tpo = world.message("Sara Khan", LI, last=True)
        assert all(s in tpo.content for s in (settings.tpo_name, settings.tpo_phone, settings.tpo_email))
        mou = world.message("Sara Khan", EM, last=True)
        assert "Memorandum of Understanding" in mou.content and mou.status == MessageStatus.APPROVED
        assert world.outreach("Sara Khan", EM).state == OutreachState.INTERESTED
        # both go through the hold window like every other message
        assert tpo.hold_until is None and mou.hold_until is None

        await world.queue(dry_run=False).run_once()  # hold + preview
        assert any(p.startswith(f"#{tpo.id} LinkedIn to Sara Khan") for p in world.previews())
        assert any(p.startswith(f"#{mou.id} Email to Sara Khan") for p in world.previews())
        assert not world.linkedin.of("reply") and not world.outlook.of("reply")  # not before the hold ends

        world.clock.advance(minutes=11)
        await world.queue(dry_run=False).run_once()
        replies = world.linkedin.of("reply")
        assert len(replies) == 1 and replies[0][1] == profile
        assert settings.tpo_name in replies[0][2] and settings.tpo_phone in replies[0][2]
        li_delays = [c.args[0] for c in world.sleep.await_args_list if 180 <= c.args[0] <= 720]
        assert li_delays  # LinkedIn auto-replies wait 3-12 minutes
        emails = world.outlook.of("reply")
        assert len(emails) == 1 and emails[0][1] == "careers@initech-robotics.test"
        assert emails[0][3] == "MoU proposal: AI ConnectX x Initech Robotics" and "Memorandum of Understanding" in emails[0][2]

    @pytest.mark.asyncio
    async def test_a_pending_cold_email_to_an_interested_lead_is_cancelled(self, world):
        await self.get_to_dm_sent(world)
        cold = world.message("Sara Khan", EM)  # never sent: the 14-day rule held it back
        assert cold.status == MessageStatus.APPROVED
        profile = "https://www.linkedin.com/in/sara-khan-demo"
        world.linkedin.incoming = [IncomingReply("li-r2", profile, profile, "Interested!", None)]
        world.clock.advance(minutes=30)
        await world.replies().run_once(check_connections=False)
        cold = world.db.get(Message, cold.id)
        assert cold.status == MessageStatus.REJECTED and cold.review_status == ReviewStatus.CANCELLED

        for _ in range(2):
            await world.queue(dry_run=False).run_once()
            world.clock.advance(minutes=11)
        cold_sent = [c for c in world.outlook.of("send")]
        assert cold_sent == []  # the only email Sara gets is the MoU reply

    @pytest.mark.asyncio
    async def test_interested_reply_in_dry_run_sends_nothing(self, world):
        await self.get_to_dm_sent(world)
        profile = "https://www.linkedin.com/in/sara-khan-demo"
        world.linkedin.incoming = [IncomingReply("li-r3", profile, profile, "Sounds good, interested.", None)]
        world.clock.advance(minutes=30)
        await world.replies().run_once(check_connections=False)
        delivered = len(world.linkedin.calls), len(world.outlook.calls)
        await world.queue().run_once()  # dry run from the settings
        world.clock.advance(minutes=11)
        await world.queue().run_once()
        assert (len(world.linkedin.calls), len(world.outlook.calls)) == delivered
