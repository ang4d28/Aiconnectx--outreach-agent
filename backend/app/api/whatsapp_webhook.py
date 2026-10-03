"""WhatsApp webhook: Meta verification handshake and owner commands."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.channels.whatsapp import normalize_number, verify_signature, whatsapp_client
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@dataclass(frozen=True)
class Command:
    name: str  # SEND, CANCEL, EDIT, STOP, START, STATUS
    message_id: int | None = None
    text: str | None = None


def parse_command(text: str) -> Command | None:
    parts = text.strip().split(None, 2)
    if not parts:
        return None
    name = parts[0].upper()
    if name in ("STOP", "START", "STATUS") and len(parts) == 1:
        return Command(name)
    if name in ("SEND", "CANCEL", "EDIT") and len(parts) >= 2 and parts[1].isdigit():
        message_id = int(parts[1])
        if name == "EDIT":
            if len(parts) < 3 or not parts[2].strip():
                return None
            return Command(name, message_id, parts[2].strip())
        if len(parts) == 2:
            return Command(name, message_id)
    return None


class CommandHandler(Protocol):
    async def send(self, message_id: int) -> str: ...
    async def cancel(self, message_id: int) -> str: ...
    async def edit(self, message_id: int, text: str) -> str: ...
    async def stop(self) -> str: ...
    async def start(self) -> str: ...
    async def status(self) -> str: ...


class StubHandler:
    """Placeholder handlers until the review flow is wired to the send queue."""

    async def send(self, message_id: int) -> str:
        return f"SEND {message_id}: not implemented yet"

    async def cancel(self, message_id: int) -> str:
        return f"CANCEL {message_id}: not implemented yet"

    async def edit(self, message_id: int, text: str) -> str:
        return f"EDIT {message_id}: not implemented yet"

    async def stop(self) -> str:
        return "STOP: not implemented yet"

    async def start(self) -> str:
        return "START: not implemented yet"

    async def status(self) -> str:
        return "STATUS: not implemented yet"


handler: CommandHandler = StubHandler()

HELP_TEXT = "Commands: SEND <id>, CANCEL <id>, EDIT <id> <text>, STOP, START, STATUS"


def set_handler(new_handler: CommandHandler) -> None:
    global handler
    handler = new_handler


async def dispatch(command: Command, target: CommandHandler | None = None) -> str:
    target = target or handler
    match command.name:
        case "SEND":
            return await target.send(command.message_id)
        case "CANCEL":
            return await target.cancel(command.message_id)
        case "EDIT":
            return await target.edit(command.message_id, command.text)
        case "STOP":
            return await target.stop()
        case "START":
            return await target.start()
        case "STATUS":
            return await target.status()
    return HELP_TEXT


def extract_owner_messages(payload: dict) -> list[dict]:
    """Messages from the owner only; everything else is dropped."""
    owner = normalize_number(settings.whatsapp_owner_number)
    found: list[dict] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for msg in (change.get("value") or {}).get("messages") or []:
                if owner and normalize_number(msg.get("from", "")) == owner:
                    found.append(msg)
    return found


@router.get("/webhook")
async def verify(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    if mode == "subscribe" and settings.whatsapp_verify_token and token == settings.whatsapp_verify_token:
        return Response(content=challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/webhook")
async def receive(request: Request) -> dict[str, str]:
    body = await request.body()
    if not verify_signature(settings.whatsapp_app_secret, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json")

    for msg in extract_owner_messages(payload):
        try:
            ts = datetime.fromtimestamp(int(msg["timestamp"]), tz=timezone.utc)
        except (KeyError, ValueError, TypeError):
            ts = None
        whatsapp_client.record_inbound(ts)
        if msg.get("type") != "text":
            continue
        text = (msg.get("text") or {}).get("body", "")
        command = parse_command(text)
        reply = await dispatch(command) if command else f"Unknown command. {HELP_TEXT}"
        try:
            await whatsapp_client.send_text(reply)
        except Exception:
            logger.exception("failed to send WhatsApp reply")
    # Always 200 so Meta does not retry.
    return {"status": "ok"}
