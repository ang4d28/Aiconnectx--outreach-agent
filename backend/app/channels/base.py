from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Thread:
    recipient: str
    subject: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True)
class IncomingReply:
    message_id: str
    thread_id: str | None
    sender: str | None
    text: str
    received_at: str | None


@runtime_checkable
class Channel(Protocol):
    async def send_message(self, thread: Thread, text: str) -> str | None: ...

    async def read_new_replies(self, since: datetime) -> list[IncomingReply]: ...

    async def reply(self, thread: Thread, text: str) -> str | None: ...
