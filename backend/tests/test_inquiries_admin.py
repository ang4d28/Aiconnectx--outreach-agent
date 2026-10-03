from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.main import api_router
from app.auth import oauth, security
from app.db import get_session
from app.db.models import AuthProvider, Inquiry, User, UserRole

CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    from argon2 import PasswordHasher

    monkeypatch.setattr(security, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    security._dummy_hash.cache_clear()
    yield
    security._dummy_hash.cache_clear()


@pytest.fixture
def app(db):
    application = FastAPI()
    oauth.install_session_middleware(application)
    application.include_router(api_router)
    application.dependency_overrides[get_session] = lambda: db
    return application


def client_as(app, db, role: UserRole) -> TestClient:
    user = User(
        email=f"{role.value}@example.test",
        name=role.value,
        role=role,
        provider=AuthProvider.PASSWORD,
        password_hash=security.hash_password("correct horse battery"),
    )
    db.add(user)
    db.commit()
    http = TestClient(app, follow_redirects=False, headers=CSRF)
    token, _ = security.create_session_token(user)
    http.cookies.set(security.SESSION_COOKIE, token)
    return http


@pytest.fixture
def inquiries(db):
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = [
        Inquiry(name="Old", email="old@example.test", interest="hire", message="First message here", status="closed", created_at=base),
        Inquiry(name="Mid", email="mid@example.test", interest="partner", message="Second message here", created_at=base + timedelta(days=1)),
        Inquiry(name="New", email="new@example.test", interest="other", message="Third message here", created_at=base + timedelta(days=2)),
    ]
    db.add_all(rows)
    db.commit()
    return rows


class TestListInquiries:
    def test_newest_first_and_paginated(self, app, db, inquiries):
        http = client_as(app, db, UserRole.VIEWER)
        body = http.get("/api/inquiries?page_size=2").json()
        assert [i["name"] for i in body["items"]] == ["New", "Mid"]
        assert body["total"] == 3 and body["page_size"] == 2
        assert [i["name"] for i in http.get("/api/inquiries?page_size=2&page=2").json()["items"]] == ["Old"]

    def test_filter_by_status_and_new_count(self, app, db, inquiries):
        http = client_as(app, db, UserRole.VIEWER)
        assert [i["name"] for i in http.get("/api/inquiries?status=closed").json()["items"]] == ["Old"]
        assert http.get("/api/inquiries?status=bogus").status_code == 422
        assert http.get("/api/inquiries/new-count").json() == {"new": 2}

    def test_requires_sign_in(self, app, inquiries):
        assert TestClient(app).get("/api/inquiries").status_code == 401


class TestUpdateInquiry:
    def test_member_sets_status_and_note(self, app, db, inquiries):
        http = client_as(app, db, UserRole.MEMBER)
        target = inquiries[2]
        r = http.patch(f"/api/inquiries/{target.id}", json={"status": "contacted", "notes": " Called on Monday "})
        assert r.status_code == 200
        assert r.json()["status"] == "contacted" and r.json()["notes"] == "Called on Monday"
        db.refresh(target)
        assert (target.status, target.notes) == ("contacted", "Called on Monday")
        assert http.get("/api/inquiries/new-count").json() == {"new": 1}

    def test_note_only_keeps_status(self, app, db, inquiries):
        http = client_as(app, db, UserRole.MEMBER)
        r = http.patch(f"/api/inquiries/{inquiries[1].id}", json={"notes": "Follow up"})
        assert r.json()["status"] == "new" and r.json()["notes"] == "Follow up"

    def test_rejects_unknown_status_and_missing_row(self, app, db, inquiries):
        http = client_as(app, db, UserRole.MEMBER)
        assert http.patch(f"/api/inquiries/{inquiries[0].id}", json={"status": "spam"}).status_code == 422
        assert http.patch("/api/inquiries/9999", json={"status": "closed"}).status_code == 404

    def test_viewer_cannot_write(self, app, db, inquiries):
        http = client_as(app, db, UserRole.VIEWER)
        assert http.patch(f"/api/inquiries/{inquiries[0].id}", json={"status": "new"}).status_code == 403
