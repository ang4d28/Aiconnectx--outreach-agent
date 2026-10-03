"""WhatsApp Cloud API client (owner notifications and message review only).

Never used to contact leads. Free-form text is only allowed inside the 24h
customer-service window that opens when the owner last messaged us; outside it
a pre-approved template must be used.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v21.0"
SERVICE_WINDOW = timedelta(hours=24)
FALLBACK_TEMPLATE = "owner_notification"
TEMPLATE_LANGUAGE = "en_US"
TIMEOUT_SECONDS = 20.0


class WhatsAppError(Exception):
    pass


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check X-Hub-Signature-256 ("sha256=<hexdigest>") over the raw body."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def normalize_number(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


class WhatsAppClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self.last_inbound_at: datetime | None = None

    def record_inbound(self, at: datetime | None = None) -> None:
        self.last_inbound_at = at or datetime.now(timezone.utc)

    def window_open(self, now: datetime | None = None) -> bool:
        if self.last_inbound_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        return now - self.last_inbound_at < SERVICE_WINDOW

    async def _post(self, payload: dict) -> str | None:
        url = f"{GRAPH_URL}/{settings.whatsapp_phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}
        body = {
            "messaging_product": "whatsapp",
            "to": normalize_number(settings.whatsapp_owner_number),
            **payload,
        }
        if self._client is not None:
            resp = await self._client.post(url, json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise WhatsAppError(f"WhatsApp API {resp.status_code}: {resp.text}")
        messages = resp.json().get("messages") or []
        return messages[0].get("id") if messages else None

    async def send_template(self, name: str, params: list[str] | None = None) -> str | None:
        template: dict = {"name": name, "language": {"code": TEMPLATE_LANGUAGE}}
        if params:
            template["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
            ]
        return await self._post({"type": "template", "template": template})

    async def send_text(self, text: str) -> str | None:
        """Free text inside the 24h window, otherwise the fallback template."""
        if self.window_open():
            return await self._post({"type": "text", "text": {"body": text, "preview_url": False}})
        # Template body parameters cannot contain newlines or runs of spaces.
        flat = " ".join(text.split())
        return await self.send_template(FALLBACK_TEMPLATE, [flat])


whatsapp_client = WhatsAppClient()


async def send_text(text: str) -> str | None:
    return await whatsapp_client.send_text(text)


async def send_template(name: str, params: list[str] | None = None) -> str | None:
    return await whatsapp_client.send_template(name, params)
