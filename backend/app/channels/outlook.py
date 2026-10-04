from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.channels.base import IncomingReply, Thread
from app.config import ENV_FILE, resolve_path
from app.sandbox import check_email

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Send", "Mail.ReadWrite"]
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER = 5.0
MAX_RETRY_AFTER = 120.0
UNSUBSCRIBE_LINE = (
    "If you'd prefer not to hear from us again, just reply \"unsubscribe\" "
    "and we will not contact you again."
)
REPLY_FIELDS = "id,conversationId,subject,from,bodyPreview,receivedDateTime"

_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class OutlookSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    ms_client_id: str = ""
    ms_tenant: str = "common"
    ms_token_cache_path: str = "secrets/ms_token.json"

    @field_validator("ms_token_cache_path")
    @classmethod
    def _cache_path(cls, value: str) -> str:
        return resolve_path(value)


class OutlookAuthError(Exception):
    pass


class GraphError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"Graph API error {status_code}: {detail}")
        self.status_code = status_code


def with_unsubscribe(body: str) -> str:
    if UNSUBSCRIBE_LINE in body:
        return body
    return f"{body.rstrip()}\n\n{UNSUBSCRIBE_LINE}"


def retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        seconds = float(raw)
    except ValueError:
        return DEFAULT_RETRY_AFTER
    return max(0.0, min(seconds, MAX_RETRY_AFTER))


def graph_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def odata_quote(value: str) -> str:
    return value.replace("'", "''")


def to_reply(raw: dict[str, Any]) -> dict[str, Any]:
    sender = (raw.get("from") or {}).get("emailAddress") or {}
    return {
        "id": raw.get("id"),
        "conversation_id": raw.get("conversationId"),
        "from": sender.get("address"),
        "subject": raw.get("subject"),
        "preview": raw.get("bodyPreview"),
        "received_at": raw.get("receivedDateTime"),
    }


def recipients_of(raw: dict[str, Any]) -> set[str]:
    return {
        (r.get("emailAddress") or {}).get("address", "").lower()
        for r in raw.get("toRecipients") or []
    }


def to_incoming(reply: dict[str, Any]) -> IncomingReply:
    return IncomingReply(
        message_id=reply["id"],
        thread_id=reply["conversation_id"],
        sender=reply["from"],
        text=reply["preview"] or "",
        received_at=reply["received_at"],
    )


def _default_app_factory(client_id: str, authority: str, cache: Any) -> Any:
    import msal

    return msal.PublicClientApplication(
        client_id, authority=authority, token_cache=cache
    )


def _default_cache_factory() -> Any:
    import msal

    return msal.SerializableTokenCache()


class TokenProvider:
    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        cache_path: str,
        app_factory: Callable[[str, str, Any], Any] = _default_app_factory,
        cache_factory: Callable[[], Any] = _default_cache_factory,
    ):
        self.cache_path = Path(cache_path)
        self.cache = cache_factory()
        self._load_cache()
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        self.app = app_factory(client_id, authority, self.cache)

    def _load_cache(self) -> None:
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text(encoding="utf-8"))

    def _save_cache(self) -> None:
        if not self.cache.has_state_changed:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(self.cache.serialize(), encoding="utf-8")

    def _silent(self, force_refresh: bool) -> dict[str, Any] | None:
        accounts = self.app.get_accounts()
        if not accounts:
            return None
        return self.app.acquire_token_silent(
            SCOPES, account=accounts[0], force_refresh=force_refresh
        )

    def _device_flow(self) -> dict[str, Any]:
        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise OutlookAuthError(f"Device flow failed: {flow.get('error_description')}")
        logger.warning(flow["message"])
        return self.app.acquire_token_by_device_flow(flow)

    def get_token(self, force_refresh: bool = False) -> str:
        result = self._silent(force_refresh)
        if not result or "access_token" not in result:
            result = self._device_flow()
        self._save_cache()
        if "access_token" not in result:
            raise OutlookAuthError(result.get("error_description", "No access token"))
        return result["access_token"]


class OutlookClient:
    def __init__(
        self,
        tokens: TokenProvider,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(timeout=30.0)
        self.sleep = sleep

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{GRAPH_BASE}{path}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        force_refresh = False
        refreshed = False
        throttled = 0
        while True:
            token = await asyncio.to_thread(self.tokens.get_token, force_refresh)
            force_refresh = False
            response = await self.http.request(
                method,
                self._url(path),
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            if response.status_code == 401 and not refreshed:
                refreshed = force_refresh = True
                continue
            if response.status_code == 429 and throttled < MAX_RETRIES:
                throttled += 1
                await self.sleep(retry_after_seconds(response))
                continue
            if response.status_code >= 400:
                raise GraphError(response.status_code, response.text[:500])
            return response

    async def send_email(
        self, to: str, subject: str, body: str, conversation_id: str | None = None
    ) -> str | None:
        check_email(to)
        if conversation_id:
            latest = await self._latest_in_conversation(conversation_id)
            if latest:
                return await self.reply_in_thread(latest, body)
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": with_unsubscribe(body)},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        await self._request("POST", "/me/sendMail", json=payload)
        return await self._find_sent_conversation(to, subject)

    async def reply_in_thread(self, message_id: str, body: str) -> str | None:
        response = await self._request(
            "POST",
            f"/me/messages/{message_id}/createReply",
            json={"comment": with_unsubscribe(body)},
        )
        draft = response.json()
        await self._request("POST", f"/me/messages/{draft['id']}/send")
        return draft.get("conversationId")

    async def list_new_replies(
        self, since: datetime, conversation_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] | None = {
            "$filter": f"receivedDateTime ge {graph_datetime(since)}",
            "$orderby": "receivedDateTime asc",
            "$select": REPLY_FIELDS,
            "$top": "50",
        }
        url: str | None = "/me/mailFolders/inbox/messages"
        replies: list[dict[str, Any]] = []
        while url:
            data = (await self._request("GET", url, params=params)).json()
            params = None
            for raw in data.get("value", []):
                if conversation_ids is None or raw.get("conversationId") in conversation_ids:
                    replies.append(to_reply(raw))
            url = data.get("@odata.nextLink")
        return replies

    async def send_message(self, thread: Thread, text: str) -> str | None:
        return await self.send_email(
            thread.recipient, thread.subject or "", text, thread.thread_id
        )

    async def read_new_replies(self, since: datetime) -> list[IncomingReply]:
        return [to_incoming(r) for r in await self.list_new_replies(since)]

    async def reply(self, thread: Thread, text: str) -> str | None:
        check_email(thread.recipient)
        if thread.thread_id:
            latest = await self._latest_in_conversation(thread.thread_id)
            if latest:
                return await self.reply_in_thread(latest, text)
        return await self.send_message(thread, text)

    async def _latest_in_conversation(self, conversation_id: str) -> str | None:
        params = {
            "$filter": f"conversationId eq '{odata_quote(conversation_id)}'",
            "$select": "id,receivedDateTime",
            "$top": "50",
        }
        data = (await self._request("GET", "/me/messages", params=params)).json()
        messages = data.get("value", [])
        if not messages:
            return None
        return max(messages, key=lambda m: m.get("receivedDateTime") or "")["id"]

    async def _find_sent_conversation(self, to: str, subject: str) -> str | None:
        params = {
            "$orderby": "sentDateTime desc",
            "$select": "conversationId,subject,toRecipients",
            "$top": "10",
        }
        try:
            response = await self._request(
                "GET", "/me/mailFolders/sentitems/messages", params=params
            )
        except GraphError as exc:
            logger.warning(f"Could not look up sent conversation: {exc}")
            return None
        for raw in response.json().get("value", []):
            if raw.get("subject") == subject and to.lower() in recipients_of(raw):
                return raw.get("conversationId")
        return None


_client: OutlookClient | None = None


def get_client() -> OutlookClient:
    global _client
    if _client is None:
        cfg = OutlookSettings()
        if not cfg.ms_client_id:
            raise OutlookAuthError("MS_CLIENT_ID is not configured")
        tokens = TokenProvider(cfg.ms_client_id, cfg.ms_tenant, cfg.ms_token_cache_path)
        _client = OutlookClient(tokens)
    return _client


async def send_email(
    to: str, subject: str, body: str, conversation_id: str | None = None
) -> str | None:
    return await get_client().send_email(to, subject, body, conversation_id)


async def reply_in_thread(message_id: str, body: str) -> str | None:
    return await get_client().reply_in_thread(message_id, body)


async def list_new_replies(
    since: datetime, conversation_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    return await get_client().list_new_replies(since, conversation_ids)
