"""Test isolation.

Before anything from `app` is imported this module:
  * overwrites every setting with a dummy value (the real environment and the real
    .env are never read) and forces DRY_RUN=true,
  * points the application at a throwaway in-memory SQLite database,
  * blocks all network access: an un-mocked call to an external service fails the
    test, even when the code under test swallows the exception.
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

_TMP = tempfile.mkdtemp(prefix="connectx-tests-")

TEST_ENV: dict[str, str] = {
    "CONNECTX_IGNORE_ENV_FILE": "1",
    "APP_ENV": "test",
    "DRY_RUN": "true",
    "BACKEND_HOST": "127.0.0.1",
    "BACKEND_PORT": "8000",
    "FRONTEND_PORT": "5173",
    # LLM providers
    "GROQ_API_KEY": "test-groq-key",
    "GROQ_MODEL": "test-groq-model",
    "GEMINI_API_KEY": "test-gemini-key",
    "GEMINI_MODEL": "test-gemini-model",
    "OLLAMA_URL": "http://ollama.invalid",
    "OLLAMA_API_KEY": "test-ollama-key",
    "OLLAMA_MODEL": "test-ollama-model",
    # LinkedIn
    "LINKEDIN_PROFILE_DIR": os.path.join(_TMP, "linkedin-profile"),
    "LINKEDIN_HEADLESS": "true",
    # Outlook / Microsoft Graph
    "MS_CLIENT_ID": "test-ms-client-id",
    "MS_TENANT": "test-tenant",
    "MS_TOKEN_CACHE_PATH": os.path.join(_TMP, "ms_token.json"),
    "OUTLOOK_SENDER_ADDRESS": "sender@outlook.invalid",
    # WhatsApp
    "WHATSAPP_PHONE_NUMBER_ID": "100000000000001",
    "WHATSAPP_TOKEN": "test-whatsapp-token",
    "WHATSAPP_VERIFY_TOKEN": "test-verify-token",
    "WHATSAPP_APP_SECRET": "test-whatsapp-app-secret",
    "WHATSAPP_OWNER_NUMBER": "919999999999",
    "HOLD_WINDOW_MINUTES": "10",
    # Handoff details
    "TPO_NAME": "Dr. Test Officer",
    "TPO_PHONE": "+91 90000 00000",
    "TPO_EMAIL": "tpo@university.invalid",
    # Dashboard auth (the OAuth client ids are dummies; the tests fake the providers)
    "JWT_SECRET": "test-jwt-secret-0123456789abcdef0123456789",
    "MS_AUTH_CLIENT_ID": "test-ms-auth-client-id",
    "MS_AUTH_CLIENT_SECRET": "test-ms-auth-client-secret",
    "MS_AUTH_TENANT": "00000000-0000-0000-0000-00000000aaaa",
    "BACKEND_URL": "https://api.connectx.test",
    "FRONTEND_URL": "https://app.connectx.test",
    # Database (never connected to: the engine is replaced by SQLite below)
    "POSTGRES_HOST": "postgres.invalid",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "connectx_test",
    "POSTGRES_USER": "connectx_test",
    "POSTGRES_PASSWORD": "test-postgres-password",
    # Search
    # Guardrails
    "LINKEDIN_DAILY_CAP": "20",
    "EMAIL_DAILY_CAP": "40",
    "SEND_WINDOW_START": "10:00",
    "SEND_WINDOW_END": "17:00",
    "SEND_WINDOW_TZ": "Asia/Kolkata",
    "MIN_TOUCH_INTERVAL_DAYS": "14",
    "DAILY_CAP_RESET_HOUR": "0",
    # Google
    "GOOGLE_SERVICE_ACCOUNT_FILE": os.path.join(_TMP, "service-account.json"),
    "GOOGLE_SHEET_ID": "test-sheet-id",
    # Sandbox off unless a test turns it on
    "SEND_ALLOWLIST": "",
}
os.environ.update(TEST_ENV)  # overwrite, never setdefault: nothing real may leak in

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import DateTime, create_engine, event  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import db as app_db  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import models  # noqa: E402

# A leaked real value would be a serious problem: refuse to run at all.
if not settings.dry_run or settings.groq_api_key != "test-groq-key" or settings.postgres_host != "postgres.invalid":
    raise pytest.UsageError("Tests are not isolated from the real environment; refusing to run.")


# ─── Throwaway database ────────────────────────────────────


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


test_engine = create_engine(
    "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}, future=True
)
# Everything that imports SessionLocal / engine from app.db gets SQLite, never Postgres.
app_db.engine = test_engine
app_db.SessionLocal.configure(bind=test_engine)


@event.listens_for(models.Base, "load", propagate=True)
@event.listens_for(models.Base, "refresh", propagate=True)
def _utc_after_load(target, *_args):  # noqa: ANN001
    """SQLite drops time zones; Postgres returns aware datetimes. Make both behave the same."""
    for column in target.__table__.columns:
        if isinstance(column.type, DateTime):
            value = target.__dict__.get(column.key)
            if isinstance(value, datetime) and value.tzinfo is None:
                target.__dict__[column.key] = value.replace(tzinfo=timezone.utc)


@pytest.fixture
def db():
    """A fresh, empty SQLite database for one test."""
    models.Base.metadata.drop_all(test_engine)
    models.Base.metadata.create_all(test_engine)
    session = Session(test_engine, expire_on_commit=True, autoflush=False)
    try:
        yield session
    finally:
        session.close()
        models.Base.metadata.drop_all(test_engine)


# ─── Network guard ─────────────────────────────────────────


class NetworkBlocked(RuntimeError):
    pass


LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "", None}


class NetworkGuard:
    """Records (and raises on) every attempt to leave the machine."""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def check(self, host: object, port: object = None) -> None:
        if isinstance(host, bytes):
            host = host.decode()
        if host in LOCAL_HOSTS:
            return
        message = f"Network call to {host}:{port} in a test. Mock the external service."
        self.violations.append(message)
        raise NetworkBlocked(message)

    def clear(self) -> None:
        self.violations.clear()


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    guard = NetworkGuard()

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):  # noqa: ANN001
        if self.family in (socket.AF_INET, socket.AF_INET6):
            guard.check(address[0], address[1])
        return real_connect(self, address)

    def connect_ex(self, address):  # noqa: ANN001
        if self.family in (socket.AF_INET, socket.AF_INET6):
            guard.check(address[0], address[1])
        return real_connect_ex(self, address)

    def getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001
        guard.check(host, port)
        return real_getaddrinfo(host, port, *args, **kwargs)

    async def blocked_async(self, request, *args, **kwargs):  # noqa: ANN001
        guard.check(request.url.host, request.url.port)

    def blocked_sync(self, request, *args, **kwargs):  # noqa: ANN001
        guard.check(request.url.host, request.url.port)

    real_create_connection = asyncio.BaseEventLoop.create_connection

    async def create_connection(self, protocol_factory, host=None, port=None, **kwargs):  # noqa: ANN001
        guard.check(host, port)
        return await real_create_connection(self, protocol_factory, host, port, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_sync)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", create_connection)

    yield guard
    # Code under test often swallows exceptions; the recorded attempt still fails the test.
    assert not guard.violations, "\n".join(guard.violations)


# ─── Shared fixtures ───────────────────────────────────────


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_llm_client():
    """Mock LLM client for testing."""
    mock_client = MagicMock()
    mock_client.complete_json = AsyncMock()
    return mock_client


@pytest.fixture
def mock_httpx_client():
    """Mock httpx AsyncClient for testing HTTP requests."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}
    mock_response.raise_for_status.return_value = None
    mock_client.post.return_value = mock_response
    return mock_client, mock_response
