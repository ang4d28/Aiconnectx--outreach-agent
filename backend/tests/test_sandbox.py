from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import cli, config, sandbox
from app.channels import outlook
from app.channels.base import IncomingReply, Thread
from app.channels.outlook import OutlookClient
from app.config import settings
from app.db.models import MessageStatus
from tests.test_send_queue import simple_setup


@pytest.fixture
def allowlist(monkeypatch):
    monkeypatch.setattr(settings, "send_allowlist", "Me@Test.com, other@test.com")


class TestAllowlist:
    def test_empty_means_normal_behaviour(self):
        assert not sandbox.active()
        assert sandbox.email_allowed("anyone@anywhere.com")

    def test_only_listed_addresses_pass_case_insensitive(self, allowlist):
        assert sandbox.active()
        assert sandbox.email_allowed("me@test.com") and sandbox.email_allowed(" OTHER@test.com ")
        assert not sandbox.email_allowed("hr@acme.com")
        assert not sandbox.email_allowed("x@test.com")  # exact addresses, not domains

    def test_check_logs_blocked_by_sandbox(self, allowlist, caplog):
        with caplog.at_level(logging.WARNING), pytest.raises(sandbox.SandboxBlocked):
            sandbox.check_email("hr@acme.com")
        assert "blocked by sandbox" in caplog.text

    def test_whatsapp_target_must_be_owner_or_listed_number(self, monkeypatch):
        monkeypatch.setattr(settings, "send_allowlist", "me@test.com, +91 98765 43210")
        monkeypatch.setattr(settings, "whatsapp_owner_number", "+1 555 000 1111")
        sandbox.check_whatsapp("919876543210")
        sandbox.check_whatsapp("+15550001111")
        with pytest.raises(sandbox.SandboxBlocked):
            sandbox.check_whatsapp("+44 20 7946 0000")

    def test_startup_line(self, allowlist, monkeypatch):
        assert "DRY_RUN=true" in sandbox.startup_line()
        assert "ACTIVE (2 address(es))" in sandbox.startup_line()
        monkeypatch.setattr(settings, "send_allowlist", "")
        assert "allowlist inactive" in sandbox.startup_line()


class TestOutlookClientGuard:
    @pytest.mark.asyncio
    async def test_send_email_blocked_before_any_request(self, allowlist):
        client = OutlookClient(tokens=MagicMock(), http=MagicMock())
        client._request = AsyncMock()
        with pytest.raises(sandbox.SandboxBlocked):
            await client.send_email("hr@acme.com", "Hi", "Body")
        with pytest.raises(sandbox.SandboxBlocked):
            await client.reply(Thread(recipient="hr@acme.com", thread_id="c1"), "Body")
        client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_address_is_sent(self, allowlist):
        client = OutlookClient(tokens=MagicMock(), http=MagicMock())
        client._request = AsyncMock()
        client._find_sent_conversation = AsyncMock(return_value="conv")
        assert await client.send_email("me@test.com", "Hi", "Body") == "conv"
        client._request.assert_awaited_once()


class TestSendQueueSandbox:
    @pytest.mark.asyncio
    async def test_unlisted_recipient_is_marked_blocked_never_sent(self, allowlist):
        h, messages, _, _ = simple_setup(2)  # p1@acme.com, p2@acme.com
        report = await h.run()
        assert report.blocked == 2 and report.sent == 0
        h.client.send_email.assert_not_called()
        assert all(m.status == MessageStatus.BLOCKED for m in messages)

    @pytest.mark.asyncio
    async def test_blocked_even_in_dry_run(self, allowlist):
        h, messages, _, _ = simple_setup(1, dry_run=True)
        report = await h.run()
        assert report.blocked == 1 and report.dry_run == 0
        assert messages[0].status == MessageStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_client_level_block_marks_blocked(self, allowlist):
        h, messages, _, contacts = simple_setup(1)
        contacts[1].email = "me@test.com"  # passes the pre-check, the client still refuses
        h.client.send_email = AsyncMock(side_effect=sandbox.SandboxBlocked("blocked by sandbox"))
        report = await h.run()
        assert report.blocked == 1
        assert messages[0].status == MessageStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_listed_recipient_is_sent(self, allowlist):
        h, messages, _, contacts = simple_setup(1)
        contacts[1].email = "me@test.com"
        report = await h.run()
        assert report.sent == 1 and messages[0].status == MessageStatus.SENT

    @pytest.mark.asyncio
    async def test_empty_allowlist_sends_normally(self):
        h, _, _, _ = simple_setup(1)
        assert (await h.run()).sent == 1


class TestPaths:
    def test_relative_resolves_from_project_root_and_creates_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "_ROOT", tmp_path)
        resolved = config.resolve_path("secrets/ms_token.json")
        assert resolved == str(tmp_path / "secrets" / "ms_token.json")
        assert (tmp_path / "secrets").is_dir()
        profile = config.resolve_path("secrets/profile", is_dir=True)
        assert (tmp_path / "secrets" / "profile").is_dir() and profile.endswith("profile")

    def test_absolute_kept(self, tmp_path):
        target = tmp_path / "x" / "tok.json"
        assert config.resolve_path(str(target)) == str(target)
        assert (tmp_path / "x").is_dir()

    def test_no_gmail_settings(self):
        assert not [name for name in config.Settings.model_fields if "gmail" in name]


class TestTestChannelCommand:
    @pytest.fixture
    def client(self, monkeypatch):
        fake = MagicMock()
        fake.send_message = AsyncMock(return_value="conv")
        fake.read_new_replies = AsyncMock(
            return_value=[IncomingReply(message_id="1", thread_id="c", sender="a@b.com", text="Thanks!", received_at="t")]
        )
        monkeypatch.setattr(outlook, "get_client", lambda: fake)
        return fake

    def test_outlook_sends_then_lists_replies(self, client, capsys):
        assert cli.main(["test-channel", "outlook", "--to", "me@test.com"]) == 0
        out = capsys.readouterr().out
        client.send_message.assert_awaited_once()
        assert "OK: test message sent" in out and "a@b.com: Thanks!" in out

    def test_outlook_respects_allowlist(self, client, allowlist, capsys):
        assert cli.main(["test-channel", "outlook", "--to", "hr@acme.com"]) == 1
        client.send_message.assert_not_called()
        assert "blocked by sandbox" in capsys.readouterr().err

    def test_prints_exact_error(self, client, capsys):
        client.send_message.side_effect = RuntimeError("Graph API error 403: nope")
        assert cli.main(["test-channel", "outlook", "--to", "me@test.com"]) == 1
        assert "RuntimeError: Graph API error 403: nope" in capsys.readouterr().err

    def test_whatsapp_sends_to_given_number(self, monkeypatch, capsys):
        from app.channels import whatsapp

        send = AsyncMock(return_value="wamid")
        monkeypatch.setattr(whatsapp.whatsapp_client, "send_text", send)
        assert cli.main(["test-channel", "whatsapp", "--to", "+911234567890"]) == 0
        assert send.await_args.kwargs["to"] == "+911234567890"
        assert "no read API" in capsys.readouterr().out
