from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import whatsapp_webhook as wh
from app.channels import whatsapp
from app.channels.whatsapp import WhatsAppClient, WhatsAppError, verify_signature
from app.config import settings

OWNER = "919876543210"
SECRET = "app-secret"
HANDLER_METHODS = ("send", "cancel", "edit", "stop", "start", "status")


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "PNID")
    monkeypatch.setattr(settings, "whatsapp_token", "tok")
    monkeypatch.setattr(settings, "whatsapp_verify_token", "verify")
    monkeypatch.setattr(settings, "whatsapp_app_secret", SECRET)
    monkeypatch.setattr(settings, "whatsapp_owner_number", "+91 98765 43210")
    monkeypatch.setattr(whatsapp.whatsapp_client, "last_inbound_at", None)


def mock_http(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = "err"
    resp.json.return_value = body if body is not None else {"messages": [{"id": "wamid.1"}]}
    http = AsyncMock()
    http.post.return_value = resp
    return http


class TestSending:
    @pytest.mark.asyncio
    async def test_template_when_window_closed(self):
        http = mock_http()
        mid = await WhatsAppClient(http).send_text("hello\nworld")
        assert mid == "wamid.1"
        args, kwargs = http.post.call_args
        assert args[0] == "https://graph.facebook.com/v21.0/PNID/messages"
        assert kwargs["headers"]["Authorization"] == "Bearer tok"
        body = kwargs["json"]
        assert body["to"] == OWNER and body["type"] == "template"
        assert body["template"]["components"][0]["parameters"][0]["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_free_text_when_window_open(self):
        http = mock_http()
        client = WhatsAppClient(http)
        client.record_inbound()
        await client.send_text("hi")
        body = http.post.call_args.kwargs["json"]
        assert body["type"] == "text" and body["text"]["body"] == "hi"

    @pytest.mark.asyncio
    async def test_window_expires_after_24h(self):
        http = mock_http()
        client = WhatsAppClient(http)
        client.record_inbound(datetime.now(timezone.utc) - timedelta(hours=25))
        assert client.window_open() is False
        await client.send_text("x")
        assert http.post.call_args.kwargs["json"]["type"] == "template"

    @pytest.mark.asyncio
    async def test_send_template_params(self):
        http = mock_http()
        await WhatsAppClient(http).send_template("tpl", ["a", "b"])
        t = http.post.call_args.kwargs["json"]["template"]
        assert t["name"] == "tpl"
        assert [p["text"] for p in t["components"][0]["parameters"]] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_api_error_raises(self):
        with pytest.raises(WhatsAppError):
            await WhatsAppClient(mock_http(status=400)).send_template("t")


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def payload(sender=OWNER, text="STATUS", mtype="text"):
    msg = {"from": sender, "type": mtype, "timestamp": "1790000000"}
    if mtype == "text":
        msg["text"] = {"body": text}
    return {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


@pytest.fixture
def app_client(monkeypatch):
    app = FastAPI()
    app.include_router(wh.router)
    sent = AsyncMock(return_value="id")
    monkeypatch.setattr(whatsapp.whatsapp_client, "send_text", sent)
    handler = MagicMock()
    for name in HANDLER_METHODS:
        setattr(handler, name, AsyncMock(return_value=f"{name} ok"))
    monkeypatch.setattr(wh, "handler", handler)
    return TestClient(app), handler, sent


def post(client, data, signature="auto"):
    body = json.dumps(data).encode()
    headers = {"X-Hub-Signature-256": sign(body) if signature == "auto" else signature}
    return client.post("/whatsapp/webhook", content=body, headers=headers)


class TestVerification:
    def test_handshake_ok(self, app_client):
        client, *_ = app_client
        r = client.get(
            "/whatsapp/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "verify", "hub.challenge": "123"},
        )
        assert r.status_code == 200 and r.text == "123"

    def test_handshake_bad_token(self, app_client):
        client, *_ = app_client
        r = client.get(
            "/whatsapp/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "123"},
        )
        assert r.status_code == 403

    def test_signature_helper(self):
        assert verify_signature(SECRET, b"x", sign(b"x"))
        assert not verify_signature(SECRET, b"x", sign(b"y"))
        assert not verify_signature(SECRET, b"x", None)
        assert not verify_signature("", b"x", sign(b"x", ""))


class TestWebhookPost:
    def test_bad_signature_rejected(self, app_client):
        client, handler, _ = app_client
        assert post(client, payload(), signature="sha256=bad").status_code == 403
        handler.status.assert_not_called()

    def test_missing_signature_rejected(self, app_client):
        client, *_ = app_client
        assert client.post("/whatsapp/webhook", json=payload()).status_code == 403

    def test_non_owner_ignored(self, app_client):
        client, handler, sent = app_client
        assert post(client, payload(sender="15551234567")).status_code == 200
        handler.status.assert_not_called()
        sent.assert_not_called()
        assert whatsapp.whatsapp_client.last_inbound_at is None

    def test_owner_command_dispatched_and_window_opened(self, app_client):
        client, handler, sent = app_client
        assert post(client, payload(text="status")).status_code == 200
        handler.status.assert_awaited_once()
        sent.assert_awaited_once_with("status ok")
        assert whatsapp.whatsapp_client.last_inbound_at is not None

    def test_unknown_command_gets_help(self, app_client):
        client, _, sent = app_client
        post(client, payload(text="hello"))
        assert "Unknown command" in sent.call_args.args[0]

    def test_non_text_ignored(self, app_client):
        client, _, sent = app_client
        post(client, payload(mtype="image"))
        sent.assert_not_called()


class TestParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("SEND 12", wh.Command("SEND", 12)),
            ("cancel 3", wh.Command("CANCEL", 3)),
            ("EDIT 5 new text here", wh.Command("EDIT", 5, "new text here")),
            ("stop", wh.Command("STOP")),
            ("Start", wh.Command("START")),
            ("STATUS", wh.Command("STATUS")),
            ("SEND", None),
            ("SEND abc", None),
            ("SEND 1 extra", None),
            ("EDIT 5", None),
            ("STOP now", None),
            ("", None),
        ],
    )
    def test_parse(self, text, expected):
        assert wh.parse_command(text) == expected

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_handler(self):
        h = MagicMock()
        for name in HANDLER_METHODS:
            setattr(h, name, AsyncMock(return_value=name))
        assert await wh.dispatch(wh.Command("EDIT", 7, "t"), h) == "edit"
        h.edit.assert_awaited_once_with(7, "t")
        assert await wh.dispatch(wh.Command("SEND", 7), h) == "send"
        h.send.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_stub_handler_default(self):
        assert "not implemented" in await wh.dispatch(wh.Command("STOP"))
