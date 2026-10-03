from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.api import inquiries
from app.api.inquiries import RateLimiter
from app.db import get_session
from app.db.models import AttentionItem, AttentionType, Inquiry

VALID = {
    "name": "  Asha Rao ",
    "email": "Asha@Example.com",
    "company": "Acme Labs",
    "phone": "+91 90000 00000",
    "interest": "partner",
    "message": "We want to hire two ML interns for the spring.",
    "website": "",
}


@pytest.fixture
def setup(monkeypatch):
    db = MagicMock()
    added: list = []
    db.add.side_effect = added.append
    db.flush.side_effect = lambda: [setattr(o, "id", 11) for o in added if isinstance(o, Inquiry)]
    notify = AsyncMock()
    monkeypatch.setattr("app.workers.review.whatsapp_notify", notify)
    monkeypatch.setattr(inquiries, "limiter", RateLimiter())
    app = FastAPI()
    app.include_router(inquiries.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: db
    return TestClient(app), db, added, notify


class TestCreateInquiry:
    def test_stores_inquiry_attention_item_and_alerts_owner(self, setup):
        client, db, added, notify = setup
        r = client.post("/api/inquiries", json=VALID)
        assert r.status_code == 201 and r.json() == {"status": "received"}
        inquiry = next(o for o in added if isinstance(o, Inquiry))
        assert (inquiry.name, inquiry.email, inquiry.interest) == ("Asha Rao", "asha@example.com", "partner")
        assert inquiry.company == "Acme Labs"
        item = next(o for o in added if isinstance(o, AttentionItem))
        assert item.type == AttentionType.NEEDS_MANUAL
        assert item.payload["inquiry_id"] == 11 and item.payload["reason"] == "New website inquiry"
        db.commit.assert_called_once()
        text = notify.await_args.args[0]
        assert text.startswith("New website inquiry #11 from Asha Rao (Acme Labs)")
        assert "asha@example.com" in text and "ML interns" in text

    def test_optional_fields_may_be_empty(self, setup):
        client, _, added, _ = setup
        r = client.post("/api/inquiries", json={**VALID, "company": "", "phone": ""})
        assert r.status_code == 201
        inquiry = next(o for o in added if isinstance(o, Inquiry))
        assert inquiry.company is None and inquiry.phone is None

    def test_honeypot_looks_successful_but_stores_nothing(self, setup):
        client, db, added, notify = setup
        r = client.post("/api/inquiries", json={**VALID, "website": "http://spam.example"})
        assert r.status_code == 201
        assert added == [] and not notify.called
        db.commit.assert_not_called()

    @pytest.mark.parametrize(
        "change",
        [
            {"name": "   "},
            {"email": "not-an-email"},
            {"email": "a@b"},
            {"message": "too short"},
            {"message": "x" * 2001},
            {"interest": "spam"},
            {"name": "x" * 121},
        ],
    )
    def test_invalid_input_rejected(self, setup, change):
        client, db, added, _ = setup
        assert client.post("/api/inquiries", json={**VALID, **change}).status_code == 422
        assert added == []

    def test_control_characters_stripped(self, setup):
        client, _, added, _ = setup
        client.post("/api/inquiries", json={**VALID, "message": "Hello\x00 there\x07, we are hiring."})
        inquiry = next(o for o in added if isinstance(o, Inquiry))
        assert "\x00" not in inquiry.message and "\x07" not in inquiry.message

    def test_alert_failure_does_not_fail_the_request(self, setup):
        client, _, _, notify = setup
        notify.side_effect = RuntimeError("whatsapp down")
        assert client.post("/api/inquiries", json=VALID).status_code == 201

    def test_rate_limited_per_client(self, setup):
        client, _, _, _ = setup
        headers = {"CF-Connecting-IP": "203.0.113.5"}
        for _ in range(5):
            assert client.post("/api/inquiries", json=VALID, headers=headers).status_code == 201
        assert client.post("/api/inquiries", json=VALID, headers=headers).status_code == 429
        other = {"CF-Connecting-IP": "203.0.113.6"}
        assert client.post("/api/inquiries", json=VALID, headers=other).status_code == 201


class TestRateLimiter:
    def test_window_slides(self):
        now = [0.0]
        limiter = RateLimiter(limit=2, window=60, clock=lambda: now[0])
        assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
        assert limiter.allow("b")
        now[0] = 61
        assert limiter.allow("a")

    def test_table_is_pruned(self, monkeypatch):
        monkeypatch.setattr(inquiries, "MAX_TRACKED_CLIENTS", 3)
        now = [0.0]
        limiter = RateLimiter(limit=1, window=10, clock=lambda: now[0])
        for name in "abcd":
            limiter.allow(name)
        now[0] = 100
        limiter.allow("e")
        assert set(limiter.hits) == {"e"}

    def test_client_id_prefers_cloudflare_header(self):
        request = MagicMock()
        request.headers = {"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "9.9.9.9"}
        assert inquiries.client_id(request) == "1.2.3.4"
        request.headers = {"x-forwarded-for": "5.6.7.8, 10.0.0.1"}
        assert inquiries.client_id(request) == "5.6.7.8"
        request.headers = {}
        request.client.host = "172.18.0.2"
        assert inquiries.client_id(request) == "172.18.0.2"


def test_table_definition_matches_migration():
    ddl = str(CreateTable(Inquiry.__table__).compile(dialect=postgresql.dialect()))
    for column in ("name", "email", "company", "phone", "interest", "message", "created_at"):
        assert column in ddl
    from importlib import import_module

    migration = import_module("app.db.alembic.versions.005_inquiries")
    assert migration.down_revision == "004_future_pool"


class TestStoredInquiry:
    """Runs the route against a real database session (no mocks) so ORM/enum mapping is exercised."""

    def test_valid_inquiry_is_stored(self, db, monkeypatch):
        monkeypatch.setattr("app.workers.review.whatsapp_notify", AsyncMock())
        monkeypatch.setattr(inquiries, "limiter", RateLimiter())
        app = FastAPI()
        app.include_router(inquiries.router, prefix="/api")
        app.dependency_overrides[get_session] = lambda: db
        r = TestClient(app).post("/api/inquiries", json=VALID)
        assert r.status_code == 201
        stored = db.query(Inquiry).one()
        assert (stored.name, stored.email, stored.interest) == ("Asha Rao", "asha@example.com", "partner")
        item = db.query(AttentionItem).one()
        assert item.type == AttentionType.NEEDS_MANUAL and item.payload["inquiry_id"] == stored.id

    def test_invalid_input_is_a_422(self, setup):
        client, _, _, _ = setup
        assert client.post("/api/inquiries", json={**VALID, "email": "nope"}).status_code == 422

    def test_postgres_enums_bind_lowercase_values(self):
        # The migrations create lowercase enum labels; the models must send those, not member names.
        col = AttentionItem.__table__.c.type.type
        assert col.enums == ["interested", "question", "channel_error", "needs_manual"]
