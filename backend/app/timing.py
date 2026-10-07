"""Request timing: one log line per API request with method, path, status, duration and SQL query count.

Requests slower than SLOW_REQUEST_MS are logged as warnings. Pure ASGI middleware (no extra task per
request) and a SQLAlchemy engine hook that counts statements for the request being served.
Only the method, path (no query string, so no filter text or tokens) and numbers are logged.
"""

from __future__ import annotations

import contextvars
import logging
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger("app.timing")

SLOW_REQUEST_MS = 300.0

_query_counter: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("query_counter", default=None)


@event.listens_for(Engine, "before_cursor_execute")
def _count_query(*_args: Any) -> None:
    counter = _query_counter.get()
    if counter is not None:
        counter[0] += 1


def configure_logging() -> None:
    """uvicorn only configures its own loggers; give this one a handler so the lines are visible."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(logging.INFO)


class TimingMiddleware:
    def __init__(self, app: Any, slow_ms: float = SLOW_REQUEST_MS) -> None:
        self.app = app
        self.slow_ms = slow_ms

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        counter = [0]
        token = _query_counter.set(counter)
        started = time.perf_counter()
        status_code = 0

        async def capture(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            _query_counter.reset(token)
            elapsed = (time.perf_counter() - started) * 1000
            line = f"{scope['method']} {scope['path']} {status_code} {elapsed:.0f} ms, {counter[0]} queries"
            if elapsed > self.slow_ms:
                logger.warning(f"SLOW {line}")
            else:
                logger.info(line)
