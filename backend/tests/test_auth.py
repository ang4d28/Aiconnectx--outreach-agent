"""Multi-user dashboard auth: passwords, Microsoft sign-in, roles, audit trail.

The providers are faked at the HTTP transport (FakeProvider): discovery document, JWKS and
token endpoint answer with ID tokens signed by a test RSA key, so authlib's real state, PKCE,
signature, issuer, audience, expiry and nonce checks all run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from alembic.script import ScriptDirectory
from argon2 import PasswordHasher
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.db as app_db_package
from app import cli
from app.api import auth as auth_api
from app.api.main import api_router
from app.auth import deps, oauth, security
from app.config import settings
from app.db import get_session
from app.db.models import (
    AttentionItem,
    AttentionStatus,
    AttentionType,
    AuthProvider,
    ChannelType,
    Company,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    Outreach,
    OutreachState,
    ReviewStatus,
    User,
    UserRole,
)
from app.workers import review

PASSWORD = "correct horse battery"
CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}
FRONTEND = "https://app.connectx.test"


# ─── Fixtures and helpers ──────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch):
    """Cheap argon2 for speed, fresh rate limiters, no cached OAuth registry."""
    monkeypatch.setattr(security, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    security._dummy_hash.cache_clear()
    monkeypatch.setattr(auth_api, "limiters", auth_api.AuthLimiters())
    oauth.reset_registry()
    yield
    oauth.reset_registry()
    security._dummy_hash.cache_clear()


@pytest.fixture
def app(db):
    application = FastAPI()
    oauth.install_session_middleware(application)
    application.include_router(api_router)
    application.dependency_overrides[get_session] = lambda: db
    return application


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False, headers=CSRF)


@pytest.fixture
def bare(app):
    """A client that does not send the CSRF header."""
    return TestClient(app, follow_redirects=False)


def add_user(
    db,
    email="member@example.test",
    role=UserRole.MEMBER,
    provider=AuthProvider.PASSWORD,
    password=PASSWORD,
    subject=None,
    name="Test User",
):
    user = User(
        email=email,
        name=name,
        role=role,
        provider=provider,
        provider_subject=subject,
        password_hash=security.hash_password(password) if provider == AuthProvider.PASSWORD else None,
    )
    db.add(user)
    db.commit()
    return user


def client_for(app, user, **claims) -> TestClient:
    """A client holding a valid session cookie for `user` (skips the login route)."""
    http = TestClient(app, follow_redirects=False, headers=CSRF)
    token, _ = security.create_session_token(user)
    http.cookies.set(security.SESSION_COOKIE, token)
    return http


def forged_cookie(client: TestClient, user, secret=None, **overrides) -> None:
    claims = {
        "sub": str(user.id),
        "pv": security.password_fingerprint(user),
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        **overrides,
    }
    claims = {key: value for key, value in claims.items() if value is not None}
    client.cookies.set(security.SESSION_COOKIE, jwt.encode(claims, secret or settings.jwt_secret, algorithm="HS256"))


def add_draft(db, status=MessageStatus.DRAFTED) -> Message:
    company = Company(name="Acme")
    db.add(company)
    db.flush()
    contact = Contact(company_id=company.id, name="Jane Doe")
    db.add(contact)
    db.flush()
    outreach = Outreach(contact_id=contact.id, channel=ChannelType.LINKEDIN, state=OutreachState.DRAFTED)
    db.add(outreach)
    db.flush()
    message = Message(
        outreach_id=outreach.id, direction=MessageDirection.OUTBOUND, content="Hello Jane", status=status
    )
    db.add(message)
    db.commit()
    return message


def session_cookie_headers(response) -> list[str]:
    return [h for h in response.headers.get_list("set-cookie") if h.startswith(security.SESSION_COOKIE)]


# ─── Password helpers ──────────────────────────────────────


class TestPasswordHelpers:
    def test_hash_is_argon2id_and_verifies(self):
        hashed = security.hash_password(PASSWORD)
        assert hashed.startswith("$argon2id$") and PASSWORD not in hashed
        assert security.verify_password(hashed, PASSWORD)
        assert not security.verify_password(hashed, PASSWORD + "x")

    def test_unknown_account_still_runs_a_verification(self, monkeypatch):
        calls = []
        real = security._hasher

        class Spy:
            def verify(self, stored, password):
                calls.append(stored)
                return real.verify(stored, password)

            def hash(self, password):
                return real.hash(password)

        monkeypatch.setattr(security, "_hasher", Spy())
        assert security.verify_password(None, PASSWORD) is False
        assert len(calls) == 1  # the dummy hash was checked, so timing matches a wrong password

    def test_garbage_hash_is_a_failed_login_not_an_error(self):
        assert security.verify_password("not-a-hash", PASSWORD) is False

    def test_production_hasher_uses_argon2_defaults(self):
        hasher = PasswordHasher()
        assert hasher.hash("x" * 12).startswith("$argon2id$")


# ─── Registration ──────────────────────────────────────────


class TestRegister:
    body = {"email": "New.Person@Example.test", "name": "New Person", "password": PASSWORD}

    def test_creates_a_pending_account_with_a_hashed_password(self, client, db):
        r = client.post("/api/auth/register", json=self.body)
        assert r.status_code == 202 and r.json()["status"] == "pending_approval"
        user = db.scalar(select(User))
        assert user.email == "new.person@example.test"
        assert user.role == UserRole.PENDING and user.provider == AuthProvider.PASSWORD
        assert user.password_hash.startswith("$argon2id$") and PASSWORD not in user.password_hash
        assert user.provider_subject is None
        assert session_cookie_headers(r) == []  # registering does not sign anyone in

    @pytest.mark.parametrize("password", ["short", "ninechars", "x" * 129])
    def test_password_length_limits(self, client, db, password):
        r = client.post("/api/auth/register", json={**self.body, "password": password})
        assert r.status_code == 422
        assert db.scalar(select(User)) is None

    def test_ten_characters_is_enough(self, client):
        assert client.post("/api/auth/register", json={**self.body, "password": "0123456789"}).status_code == 202

    @pytest.mark.parametrize("email", ["nope", "a@b", "a b@c.test", ""])
    def test_invalid_email(self, client, email):
        assert client.post("/api/auth/register", json={**self.body, "email": email}).status_code == 422

    def test_taken_email_looks_identical_and_changes_nothing(self, client, db):
        existing = add_user(db, "new.person@example.test", UserRole.ADMIN)
        original_hash = existing.password_hash
        r = client.post("/api/auth/register", json={**self.body, "password": "a-different-password"})
        first = client.post("/api/auth/register", json={**self.body, "email": "someone.else@example.test"})
        assert r.status_code == first.status_code == 202 and r.json() == first.json()
        db.refresh(existing)
        assert existing.role == UserRole.ADMIN and existing.password_hash == original_hash
        assert db.scalar(select(User).where(User.email == "new.person@example.test")).id == existing.id

    def test_register_requires_csrf_header(self, bare, db):
        assert bare.post("/api/auth/register", json=self.body).status_code == 403
        assert db.scalar(select(User)) is None

    def test_rate_limited_per_ip(self, client):
        for i in range(5):
            assert client.post("/api/auth/register", json={**self.body, "email": f"p{i}@example.test"}).status_code == 202
        r = client.post("/api/auth/register", json={**self.body, "email": "p5@example.test"})
        assert r.status_code == 429 and r.headers["retry-after"] == "60"

    def test_rate_limited_per_email_across_ips(self, client):
        for i in range(5):
            r = client.post("/api/auth/register", json=self.body, headers={"X-Forwarded-For": f"10.0.0.{i}"})
            assert r.status_code == 202
        r = client.post("/api/auth/register", json=self.body, headers={"X-Forwarded-For": "10.0.0.99"})
        assert r.status_code == 429


# ─── Login ─────────────────────────────────────────────────


class TestLogin:
    def login(self, client, email="member@example.test", password=PASSWORD, **kwargs):
        return client.post("/api/auth/login", json={"email": email, "password": password}, **kwargs)

    def test_success_sets_an_http_only_lax_cookie_and_no_token_in_the_body(self, client, db):
        user = add_user(db)
        r = self.login(client)
        assert r.status_code == 200
        assert r.json()["user"]["email"] == "member@example.test" and r.json()["user"]["role"] == "member"
        assert "access_token" not in r.text and "password_hash" not in r.text and PASSWORD not in r.text
        (cookie,) = session_cookie_headers(r)
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
        assert f"Max-Age={12 * 3600}" in cookie and "Secure" not in cookie
        db.refresh(user)
        assert user.last_login_at is not None
        claims = jwt.decode(client.cookies[security.SESSION_COOKIE], settings.jwt_secret, algorithms=["HS256"])
        assert claims["sub"] == str(user.id) and claims["exp"] - claims["iat"] == 12 * 3600

    def test_email_is_case_insensitive(self, client, db):
        add_user(db)
        assert self.login(client, email="  MEMBER@Example.TEST ").status_code == 200

    def test_cookie_is_secure_in_production(self, client, db, monkeypatch):
        add_user(db)
        monkeypatch.setattr(settings, "app_env", "production")
        (cookie,) = session_cookie_headers(self.login(client))
        assert "Secure" in cookie and "HttpOnly" in cookie

    def test_wrong_password_unknown_email_and_disabled_all_look_the_same(self, client, db):
        add_user(db, "member@example.test")
        add_user(db, "off@example.test", UserRole.DISABLED)
        add_user(db, "google@example.test", provider=AuthProvider.GOOGLE, subject="g-1")
        responses = [
            self.login(client, password="wrong password!"),
            self.login(client, email="nobody@example.test"),
            self.login(client, email="off@example.test"),  # right password, disabled account
            self.login(client, email="google@example.test"),  # exists only as a Google account
        ]
        assert {r.status_code for r in responses} == {401}
        assert len({r.text for r in responses}) == 1 and "Invalid email or password" in responses[0].text
        assert all(session_cookie_headers(r) == [] for r in responses)

    def test_pending_account_can_sign_in_but_only_see_itself(self, client, db):
        add_user(db, "wait@example.test", UserRole.PENDING)
        assert self.login(client, "wait@example.test").status_code == 200
        assert client.get("/api/auth/me").json()["role"] == "pending"
        assert client.get("/api/dashboard/status").status_code == 403

    def test_login_requires_csrf_header(self, bare, db):
        add_user(db)
        assert self.login(bare).status_code == 403

    def test_rate_limited_per_ip_even_with_the_right_password(self, client, db):
        add_user(db)
        for i in range(5):
            assert self.login(client, email=f"guess{i}@example.test").status_code == 401
        r = self.login(client)
        assert r.status_code == 429 and r.headers["retry-after"] == "60"
        assert session_cookie_headers(r) == []

    def test_rate_limited_per_email_across_ips(self, client, db):
        add_user(db)
        for i in range(5):
            r = self.login(client, password="wrong password!", headers={"X-Forwarded-For": f"10.1.0.{i}"})
            assert r.status_code == 401
        r = self.login(client, headers={"X-Forwarded-For": "10.1.0.99"})
        assert r.status_code == 429

    def test_old_hash_parameters_are_upgraded_on_login(self, client, db, monkeypatch):
        user = add_user(db)
        old = user.password_hash
        monkeypatch.setattr(security, "_hasher", PasswordHasher(time_cost=2, memory_cost=16, parallelism=1))
        assert self.login(client).status_code == 200
        db.refresh(user)
        assert user.password_hash != old and security.verify_password(user.password_hash, PASSWORD)

    @pytest.mark.parametrize("secret", ["", "short"])
    def test_fails_closed_without_a_jwt_secret(self, client, db, monkeypatch, secret):
        add_user(db)
        monkeypatch.setattr(settings, "jwt_secret", secret)
        assert self.login(client).status_code == 503
        assert client.post("/api/auth/register", json=TestRegister.body).status_code == 503
        assert client.get("/api/auth/me").status_code == 503
        assert client.get("/api/auth/microsoft/login").status_code == 503


# ─── Sessions ──────────────────────────────────────────────


class TestSession:
    def test_me_without_a_cookie(self, client):
        assert client.get("/api/auth/me").status_code == 401

    def test_me_returns_the_account_without_secrets(self, app, db):
        user = add_user(db)
        body = client_for(app, user).get("/api/auth/me").json()
        assert body["email"] == user.email and body["provider"] == "password" and body["role"] == "member"
        assert "password_hash" not in body and "password" not in body

    def test_bearer_header_is_not_a_session(self, client, db):
        user = add_user(db)
        token, _ = security.create_session_token(user)
        assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_rejects_forged_expired_and_malformed_cookies(self, client, db):
        user = add_user(db)
        attempts = {
            "wrong secret": lambda: forged_cookie(client, user, secret="y" * 40),
            "expired": lambda: forged_cookie(client, user, exp=1),
            "no exp": lambda: forged_cookie(client, user, exp=None),
            "no pv": lambda: forged_cookie(client, user, pv=None),
            "wrong pv": lambda: forged_cookie(client, user, pv="0" * 16),
            "bad sub": lambda: forged_cookie(client, user, sub="abc"),
            "unknown user": lambda: forged_cookie(client, user, sub="999"),
            "garbage": lambda: client.cookies.set(security.SESSION_COOKIE, "garbage"),
        }
        for name, install in attempts.items():
            client.cookies.clear()
            install()
            assert client.get("/api/auth/me").status_code == 401, name
        client.cookies.clear()
        none_alg = jwt.encode({"sub": str(user.id), "pv": "x", "exp": 9999999999}, None, algorithm="none")
        client.cookies.set(security.SESSION_COOKIE, none_alg)
        assert client.get("/api/auth/me").status_code == 401

    def test_logout_clears_the_cookie(self, client, db):
        add_user(db)
        client.post("/api/auth/login", json={"email": "member@example.test", "password": PASSWORD})
        assert client.get("/api/auth/me").status_code == 200
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        (cookie,) = session_cookie_headers(r)
        assert "Max-Age=0" in cookie and "HttpOnly" in cookie
        assert client.get("/api/auth/me").status_code == 401

    def test_logout_requires_csrf_header(self, bare):
        assert bare.post("/api/auth/logout").status_code == 403

    def test_pending_user_is_locked_out_of_everything_but_me(self, app, db):
        http = client_for(app, add_user(db, "wait@example.test", UserRole.PENDING))
        assert http.get("/api/auth/me").json()["role"] == "pending"
        for method, path in [
            ("GET", "/api/dashboard/status"),
            ("GET", "/api/dashboard/overview"),
            ("GET", "/api/drafts"),
            ("GET", "/api/attention"),
            ("GET", "/api/export.xlsx"),
            ("POST", "/api/dashboard/pause"),
            ("POST", "/api/drafts/1/approve"),
            ("GET", "/api/admin/users"),
        ]:
            r = http.request(method, path)
            assert r.status_code == 403, (method, path)
        assert "approval" in http.get("/api/drafts").json()["detail"]

    def test_disabling_ends_an_existing_session_immediately(self, app, db):
        user = add_user(db)
        http = client_for(app, user)
        assert http.get("/api/dashboard/status").status_code == 200
        user.role = UserRole.DISABLED
        db.commit()
        assert http.get("/api/dashboard/status").status_code == 403
        assert http.post("/api/attention/1/dismiss").status_code == 403
        assert http.get("/api/auth/me").json()["role"] == "disabled"

    def test_role_changes_apply_to_the_next_request(self, app, db):
        user = add_user(db, role=UserRole.VIEWER)
        http = client_for(app, user)
        assert http.post("/api/attention/1/dismiss").status_code == 403
        user.role = UserRole.MEMBER
        db.commit()
        assert http.post("/api/attention/1/dismiss").status_code == 404  # allowed now; the item does not exist

    def test_public_routes_stay_public(self, client):
        assert client.get("/api/health").json() == {"status": "ok"}
        r = client.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x"})
        assert r.status_code == 403  # the webhook's own check, not the login
        assert client.post("/api/inquiries", json={"name": "A"}).status_code == 422  # validation, not 401


# ─── Roles and CSRF ────────────────────────────────────────


FORBIDDEN_FOR_VIEWER = [
    ("POST", "/api/attention/1/dismiss", None),
    ("POST", "/api/attention/1/approve", None),
    ("PATCH", "/api/attention/1", {"status": "resolved"}),
    ("POST", "/api/dashboard/drafts/1/approve", None),
    ("POST", "/api/dashboard/drafts/1/cancel", None),
    ("PATCH", "/api/dashboard/drafts/1", {"text": "x"}),
    ("POST", "/api/dashboard/drafts/bulk-approve", {"ids": [1]}),
    ("POST", "/api/drafts/1/approve", None),
    ("POST", "/api/drafts/1/reject", None),
    ("PATCH", "/api/drafts/1", {"content": "x"}),
]
ADMIN_ONLY = [
    ("PUT", "/api/dashboard/settings", {"values": {"hold_window_minutes": 15}}),
    ("PUT", "/api/dashboard/search-queries", {"queries": ["q"]}),
    ("POST", "/api/dashboard/pause", {"paused": True}),
    ("POST", "/api/dashboard/channels/linkedin/enable", None),
    ("POST", "/api/companies/", {"name": "Acme"}),
    ("POST", "/api/settings", {"key": "k", "value": "v"}),
    ("DELETE", "/api/settings/k", None),
    ("GET", "/api/admin/users", None),
    ("POST", "/api/admin/users/1/approve", None),
    ("PATCH", "/api/admin/users/1/role", {"role": "viewer"}),
    ("POST", "/api/admin/users/1/disable", None),
    ("POST", "/api/admin/users/1/reset-password", None),
]
READS = ["/api/dashboard/status", "/api/dashboard/overview", "/api/attention", "/api/drafts", "/api/dashboard/leads",
         "/api/dashboard/settings", "/api/reports/today"]


class TestRoles:
    @pytest.mark.parametrize(("method", "path", "body"), FORBIDDEN_FOR_VIEWER + ADMIN_ONLY)
    def test_viewer_is_read_only(self, app, db, method, path, body):
        http = client_for(app, add_user(db, role=UserRole.VIEWER))
        assert http.request(method, path, json=body).status_code == 403

    @pytest.mark.parametrize("path", READS)
    def test_viewer_can_read(self, app, db, path):
        assert client_for(app, add_user(db, role=UserRole.VIEWER)).get(path).status_code == 200

    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ONLY)
    def test_member_cannot_touch_settings_pause_or_users(self, app, db, method, path, body):
        http = client_for(app, add_user(db, role=UserRole.MEMBER))
        assert http.request(method, path, json=body).status_code == 403

    @pytest.mark.parametrize("path", READS)
    def test_member_can_read(self, app, db, path):
        assert client_for(app, add_user(db, role=UserRole.MEMBER)).get(path).status_code == 200

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("POST", "/api/dashboard/drafts/1/approve", None),
            ("POST", "/api/dashboard/drafts/1/cancel", None),
            ("PATCH", "/api/dashboard/drafts/1", {"text": "x"}),
            ("POST", "/api/dashboard/drafts/bulk-approve", {"ids": [1]}),
            ("POST", "/api/drafts/1/approve", None),
            ("POST", "/api/attention/1/dismiss", None),
            ("PATCH", "/api/attention/1", {"status": "resolved"}),
        ],
    )
    def test_member_passes_the_permission_check_for_draft_and_attention_actions(self, app, db, method, path, body):
        r = client_for(app, add_user(db)).request(method, path, json=body)
        assert r.status_code in (200, 404), r.text  # allowed; the row simply does not exist

    def test_admin_can_do_admin_things(self, app, db):
        http = client_for(app, add_user(db, "boss@example.test", UserRole.ADMIN))
        assert http.post("/api/dashboard/pause", json={"paused": True}).json() == {"paused": True}
        assert http.get("/api/admin/users").status_code == 200
        assert http.get("/api/dashboard/status").json()["paused"] is True

    def test_every_protected_route_has_the_permission_dependency(self):
        public = ("/api/auth", "/api/inquiries", "/api/whatsapp", "/api/health")
        unguarded = [
            f"{sorted(route.methods)} {route.path}"
            for route in api_router.routes
            if isinstance(route, APIRoute)
            and not route.path.startswith(public)
            and not any(dep.dependency is deps.authorize for dep in route.dependencies)
        ]
        assert unguarded == []

    def test_member_action_table_matches_real_routes(self):
        real = {(m, r.path) for r in api_router.routes if isinstance(r, APIRoute) for m in r.methods}
        assert deps.MEMBER_ACTIONS <= real  # a renamed route must not silently become admin-only

    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("GET", "/api/anything/new", UserRole.VIEWER),
            ("POST", "/api/anything/new", UserRole.ADMIN),  # a new write route is admin-only by default
            ("DELETE", "/api/companies/{company_id}", UserRole.ADMIN),
            ("POST", "/api/attention/{item_id}/dismiss", UserRole.MEMBER),
            ("GET", "/api/admin/users", UserRole.ADMIN),  # even reads
        ],
    )
    def test_required_role_defaults_closed(self, method, path, expected):
        request = SimpleNamespace(
            method=method, scope={"route": SimpleNamespace(path=path)}, url=SimpleNamespace(path=path)
        )
        assert deps.required_role(request) == expected


class TestCsrf:
    @pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.ADMIN])
    def test_writes_need_the_custom_header(self, app, db, role):
        user = add_user(db, role=role)
        http = client_for(app, user)
        del http.headers[security.CSRF_HEADER]
        assert http.post("/api/attention/1/dismiss").status_code == 403
        assert "X-CSRF-Protection" in http.post("/api/attention/1/dismiss").text
        http.headers[security.CSRF_HEADER] = "0"
        assert http.post("/api/attention/1/dismiss").status_code == 403
        http.headers[security.CSRF_HEADER] = security.CSRF_VALUE
        assert http.post("/api/attention/1/dismiss").status_code == 404

    def test_reads_do_not_need_it(self, app, db):
        http = client_for(app, add_user(db))
        del http.headers[security.CSRF_HEADER]
        assert http.get("/api/attention").status_code == 200

    def test_permission_is_checked_before_csrf(self, app, db):
        http = client_for(app, add_user(db, role=UserRole.VIEWER))
        del http.headers[security.CSRF_HEADER]
        assert http.post("/api/dashboard/pause", json={"paused": True}).status_code == 403


# ─── Admin user management ─────────────────────────────────


class TestAdminUsers:
    @pytest.fixture
    def admin(self, db):
        return add_user(db, "boss@example.test", UserRole.ADMIN, name="Boss")

    def test_list_never_exposes_password_hashes(self, app, db, admin):
        add_user(db, "wait@example.test", UserRole.PENDING)
        add_user(db, "g@example.test", UserRole.PENDING, provider=AuthProvider.GOOGLE, subject="g-1")
        http = client_for(app, admin)
        body = http.get("/api/admin/users").json()
        assert body["total"] == 3 and [u["email"] for u in body["items"]][0] == "boss@example.test"
        assert "password_hash" not in http.get("/api/admin/users").text and "argon2" not in http.get("/api/admin/users").text
        pending = http.get("/api/admin/users", params={"role": "pending"}).json()
        assert pending["total"] == 2

    def test_approve_defaults_to_viewer(self, app, db, admin):
        user = add_user(db, "wait@example.test", UserRole.PENDING)
        r = client_for(app, admin).post(f"/api/admin/users/{user.id}/approve")
        assert r.status_code == 200 and r.json()["role"] == "viewer"
        db.refresh(user)
        assert user.role == UserRole.VIEWER

    def test_approve_with_a_role_and_the_user_can_then_work(self, app, db, admin):
        user = add_user(db, "wait@example.test", UserRole.PENDING)
        r = client_for(app, admin).post(f"/api/admin/users/{user.id}/approve", json={"role": "member"})
        assert r.json()["role"] == "member"
        assert client_for(app, user).get("/api/dashboard/status").status_code == 200

    @pytest.mark.parametrize("role", ["pending", "disabled", "owner", ""])
    def test_approve_only_grants_real_roles(self, app, db, admin, role):
        user = add_user(db, "wait@example.test", UserRole.PENDING)
        assert client_for(app, admin).post(f"/api/admin/users/{user.id}/approve", json={"role": role}).status_code == 422
        db.refresh(user)
        assert user.role == UserRole.PENDING

    def test_only_pending_accounts_can_be_approved(self, app, db, admin):
        member = add_user(db, "m@example.test", UserRole.MEMBER)
        assert client_for(app, admin).post(f"/api/admin/users/{member.id}/approve").status_code == 409
        assert client_for(app, admin).post("/api/admin/users/999/approve").status_code == 404

    def test_change_role(self, app, db, admin):
        member = add_user(db, "m@example.test", UserRole.MEMBER)
        http = client_for(app, admin)
        assert http.patch(f"/api/admin/users/{member.id}/role", json={"role": "viewer"}).json()["role"] == "viewer"
        for bad in ("pending", "disabled", "superuser"):
            assert http.patch(f"/api/admin/users/{member.id}/role", json={"role": bad}).status_code == 422
        db.refresh(member)
        assert member.role == UserRole.VIEWER

    def test_disabled_account_can_be_re_enabled_with_a_role(self, app, db, admin):
        off = add_user(db, "off@example.test", UserRole.DISABLED)
        r = client_for(app, admin).patch(f"/api/admin/users/{off.id}/role", json={"role": "viewer"})
        assert r.json()["role"] == "viewer"

    def test_admins_cannot_demote_or_disable_themselves(self, app, db, admin):
        http = client_for(app, admin)
        assert http.patch(f"/api/admin/users/{admin.id}/role", json={"role": "viewer"}).status_code == 400
        assert http.post(f"/api/admin/users/{admin.id}/disable").status_code == 400
        db.refresh(admin)
        assert admin.role == UserRole.ADMIN

    def test_last_admin_cannot_be_demoted_or_disabled(self, db, admin):
        from fastapi import HTTPException

        from app.api.admin_users import keep_one_admin

        with pytest.raises(HTTPException) as exc:
            keep_one_admin(db, admin, "demote")
        assert exc.value.status_code == 409
        add_user(db, "second@example.test", UserRole.ADMIN)
        keep_one_admin(db, admin, "demote")  # a second admin exists: allowed

    def test_an_admin_can_promote_and_demote_another_admin(self, app, db, admin):
        member = add_user(db, "m@example.test", UserRole.MEMBER)
        http = client_for(app, admin)
        assert http.patch(f"/api/admin/users/{member.id}/role", json={"role": "admin"}).json()["role"] == "admin"
        assert http.patch(f"/api/admin/users/{member.id}/role", json={"role": "viewer"}).json()["role"] == "viewer"

    def test_disable_locks_the_user_out_everywhere(self, app, db, admin):
        victim = add_user(db, "m@example.test", UserRole.MEMBER)
        victim_client = client_for(app, victim)
        assert victim_client.get("/api/dashboard/status").status_code == 200
        r = client_for(app, admin).post(f"/api/admin/users/{victim.id}/disable")
        assert r.json()["role"] == "disabled"
        assert victim_client.get("/api/dashboard/status").status_code == 403
        login = TestClient(app, headers=CSRF).post("/api/auth/login", json={"email": "m@example.test", "password": PASSWORD})
        assert login.status_code == 401

    def test_reset_password_with_a_chosen_password(self, app, db, admin):
        user = add_user(db, "m@example.test", UserRole.MEMBER)
        old_session = client_for(app, user)
        r = client_for(app, admin).post(f"/api/admin/users/{user.id}/reset-password", json={"password": "brand new password"})
        assert r.status_code == 200 and r.json() == {"id": user.id, "temporary_password": None}
        assert old_session.get("/api/auth/me").status_code == 401  # sessions from before the reset are dead
        fresh = TestClient(app, headers=CSRF)
        assert fresh.post("/api/auth/login", json={"email": "m@example.test", "password": PASSWORD}).status_code == 401
        assert fresh.post("/api/auth/login", json={"email": "m@example.test", "password": "brand new password"}).status_code == 200

    def test_reset_password_generates_one_when_none_is_given(self, app, db, admin):
        user = add_user(db, "m@example.test", UserRole.MEMBER)
        r = client_for(app, admin).post(f"/api/admin/users/{user.id}/reset-password")
        temporary = r.json()["temporary_password"]
        assert len(temporary) >= security.MIN_PASSWORD_LENGTH
        assert TestClient(app, headers=CSRF).post(
            "/api/auth/login", json={"email": "m@example.test", "password": temporary}
        ).status_code == 200

    def test_reset_password_validates_and_only_applies_to_password_accounts(self, app, db, admin):
        user = add_user(db, "m@example.test", UserRole.MEMBER)
        oauth_user = add_user(db, "g@example.test", UserRole.MEMBER, provider=AuthProvider.GOOGLE, subject="g-1")
        http = client_for(app, admin)
        assert http.post(f"/api/admin/users/{user.id}/reset-password", json={"password": "short"}).status_code == 422
        assert http.post(f"/api/admin/users/{oauth_user.id}/reset-password").status_code == 400
        assert http.post("/api/admin/users/999/reset-password").status_code == 404


class TestAdminLogin:
    def login(self, app, email, password=PASSWORD, admin_only=True):
        return TestClient(app, headers=CSRF).post(
            "/api/auth/login", json={"email": email, "password": password, "admin_only": admin_only}
        )

    @pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.VIEWER, UserRole.PENDING])
    def test_non_admins_get_403_and_no_session(self, app, db, role):
        add_user(db, "m@example.test", role)
        r = self.login(app, "m@example.test")
        assert r.status_code == 403 and r.json()["detail"] == "This account is not an administrator"
        assert security.SESSION_COOKIE not in r.headers.get("set-cookie", "")

    def test_admin_can_sign_in(self, app, db):
        add_user(db, "boss@example.test", UserRole.ADMIN)
        r = self.login(app, "boss@example.test")
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin"

    def test_errors_stay_generic_before_the_password_is_proven(self, app, db):
        add_user(db, "m@example.test", UserRole.MEMBER)
        assert self.login(app, "m@example.test", "wrong password").status_code == 401
        assert self.login(app, "nobody@example.test").status_code == 401

    def test_member_login_without_the_flag_is_unchanged(self, app, db):
        add_user(db, "m@example.test", UserRole.MEMBER)
        assert self.login(app, "m@example.test", admin_only=False).status_code == 200

    def test_admin_login_shares_the_rate_limit(self, app, db):
        add_user(db, "m@example.test", UserRole.MEMBER)
        codes = [self.login(app, "m@example.test", "wrong password").status_code for _ in range(6)]
        assert codes[-1] == 429


class TestLogout:
    def test_logout_clears_the_cookie_for_a_pending_user(self, app, db):
        pending = add_user(db, "wait@example.test", UserRole.PENDING)
        http = client_for(app, pending)
        assert http.get("/api/auth/me").status_code == 200
        r = http.post("/api/auth/logout")
        assert r.status_code == 200 and r.json() == {"status": "signed_out"}
        assert any(security.SESSION_COOKIE in h and ("Max-Age=0" in h or "expires" in h.lower()) for h in session_cookie_headers(r))

    def test_logout_needs_the_csrf_header(self, bare):
        assert bare.post("/api/auth/logout").status_code == 403


# ─── Audit trail ───────────────────────────────────────────


class TestAudit:
    def test_dashboard_approve_records_the_user(self, app, db):
        user = add_user(db)
        message = add_draft(db)
        r = client_for(app, user).post(f"/api/dashboard/drafts/{message.id}/approve")
        assert r.json()["result"] == "approved"
        db.refresh(message)
        assert message.status == MessageStatus.APPROVED and message.reviewed_by == user.id

    def test_dashboard_cancel_records_the_user(self, app, db):
        user = add_user(db)
        message = add_draft(db, MessageStatus.APPROVED)
        assert client_for(app, user).post(f"/api/dashboard/drafts/{message.id}/cancel").json()["result"] == "cancelled"
        db.refresh(message)
        assert message.review_status == ReviewStatus.CANCELLED and message.reviewed_by == user.id

    def test_dashboard_edit_and_bulk_approve_record_the_user(self, app, db):
        user = add_user(db)
        edited, bulk = add_draft(db), add_draft(db)
        http = client_for(app, user)
        assert http.patch(f"/api/dashboard/drafts/{edited.id}", json={"text": "Better text"}).json()["result"] == "edited"
        assert http.post("/api/dashboard/drafts/bulk-approve", json={"ids": [bulk.id]}).json()["approved"] == [bulk.id]
        db.refresh(edited)
        db.refresh(bulk)
        assert edited.content == "Better text" and edited.reviewed_by == user.id and bulk.reviewed_by == user.id

    def test_legacy_drafts_api_records_the_user(self, app, db):
        user = add_user(db)
        approved, rejected, edited = add_draft(db), add_draft(db), add_draft(db)
        http = client_for(app, user)
        assert http.post(f"/api/drafts/{approved.id}/approve").json()["status"] == "approved"
        assert http.post(f"/api/drafts/{rejected.id}/reject").json()["status"] == "rejected"
        assert http.patch(f"/api/drafts/{edited.id}", json={"content": "x"}).status_code == 200
        for message in (approved, rejected, edited):
            db.refresh(message)
            assert message.reviewed_by == user.id

    def test_attention_actions_record_the_user(self, app, db):
        user = add_user(db)
        dismissed = AttentionItem(type=AttentionType.NEEDS_MANUAL, payload={"reason": "x"})
        approved = AttentionItem(type=AttentionType.NEEDS_MANUAL, payload={"reason": "y"})
        patched = AttentionItem(type=AttentionType.NEEDS_MANUAL, payload={"reason": "z"})
        db.add_all([dismissed, approved, patched])
        db.commit()
        http = client_for(app, user)
        assert http.post(f"/api/attention/{dismissed.id}/dismiss").json()["status"] == "dismissed"
        assert http.post(f"/api/attention/{approved.id}/approve").json()["status"] == "resolved"
        assert http.patch(f"/api/attention/{patched.id}", json={"status": "resolved"}).status_code == 200
        for item in (dismissed, approved, patched):
            db.refresh(item)
            assert item.resolved_by == user.id
        assert http.patch(f"/api/attention/{patched.id}", json={"status": "open"}).status_code == 200
        db.refresh(patched)
        assert patched.status == AttentionStatus.OPEN and patched.resolved_by is None

    def test_whatsapp_and_automatic_actions_leave_no_user(self, db):
        message = add_draft(db)
        review.approve_message(db, message, review.utc_now())
        db.refresh(message)
        assert message.status == MessageStatus.APPROVED and message.reviewed_by is None


# ─── Microsoft sign-in ─────────────────────────────────────


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-key-1"
JWKS = {
    "keys": [
        {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key())),
            "kid": KID,
            "use": "sig",
            "alg": "RS256",
        }
    ]
}


class FakeProvider:
    """Microsoft's discovery document, JWKS and token endpoint."""

    def __init__(self, provider: AuthProvider):
        self.provider = provider
        self.subject = f"{provider.value}-subject-1"
        self.email = "sam@example.test"
        self.key = SIGNING_KEY
        self.overrides: dict = {}
        self.token_requests: list[dict] = []
        self.auth_headers: list[str | None] = []
        self.challenge: str | None = None

    @property
    def client_id(self) -> str:
        return settings.ms_auth_client_id

    @property
    def metadata_url(self) -> str:
        return oauth.MICROSOFT_METADATA_URL.format(tenant=settings.ms_auth_tenant)

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{settings.ms_auth_tenant}/v2.0"

    @property
    def metadata(self) -> dict:
        tenant = settings.ms_auth_tenant
        multi_tenant = tenant.lower() in oauth.MICROSOFT_MULTI_TENANT
        host = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        return {
            "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0" if multi_tenant else self.issuer,
            "authorization_endpoint": f"{host}/authorize",
            "token_endpoint": f"{host}/token",
            "jwks_uri": f"{host}/keys",
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    def id_token(self, nonce: str) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": self.subject,
            "iat": now,
            "exp": now + 600,
            "nonce": nonce,
            "email": self.email,
            "name": "Sam Student",
        }
        claims.update(self.overrides)
        claims = {key: value for key, value in claims.items() if value is not None}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": KID})

    async def handle(self, request: httpx.Request, nonce: str) -> httpx.Response | None:
        url = str(request.url)
        meta = self.metadata
        if url == self.metadata_url:
            return httpx.Response(200, json=meta, request=request)
        if url == meta["jwks_uri"]:
            return httpx.Response(200, json=JWKS, request=request)
        if url == meta["token_endpoint"] and request.method == "POST":
            self.token_requests.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            self.auth_headers.append(request.headers.get("authorization"))
            return httpx.Response(
                200,
                json={"access_token": "access", "token_type": "Bearer", "expires_in": 3600, "id_token": self.id_token(nonce)},
                request=request,
            )
        return None


class FakeProviders:
    def __init__(self) -> None:
        self.microsoft = FakeProvider(AuthProvider.MICROSOFT)
        self.nonce = ""  # the nonce of the login redirect in progress

    def get(self, provider: AuthProvider) -> FakeProvider:
        return self.microsoft


@pytest.fixture
def idp(monkeypatch):
    providers = FakeProviders()

    async def handler(self, request):  # noqa: ANN001
        for fake in (providers.microsoft,):
            response = await fake.handle(request, providers.nonce)
            if response is not None:
                return response
        raise AssertionError(f"Unexpected provider request: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handler)
    return providers


def start_login(client, idp: FakeProviders, provider: AuthProvider) -> dict[str, str]:
    r = client.get(f"/api/auth/{provider.value}/login")
    assert r.status_code == 302, r.text
    location = urlparse(r.headers["location"])
    params = {key: value[0] for key, value in parse_qs(location.query).items()}
    params["_url"] = r.headers["location"]
    idp.nonce = params["nonce"]
    idp.get(provider).challenge = params["code_challenge"]
    return params


def sign_in(client, idp: FakeProviders, provider: AuthProvider, state=None, **query):
    params = start_login(client, idp, provider)
    query = {"code": "auth-code", "state": state or params["state"], **query}
    return client.get(f"/api/auth/{provider.value}/callback", params=query)


def landed_ok(r) -> bool:
    return r.status_code == 302 and r.headers["location"] == f"{FRONTEND}/dashboard" and bool(session_cookie_headers(r))


def landed_with_error(r, code: str) -> bool:
    return (
        r.status_code == 302
        and r.headers["location"] == f"{FRONTEND}/dashboard?auth_error={code}"
        and session_cookie_headers(r) == []
    )


PROVIDER_PARAMS = pytest.mark.parametrize("provider", [AuthProvider.MICROSOFT], ids=lambda p: p.value)


@PROVIDER_PARAMS
class TestOAuthSignIn:
    def test_login_redirects_with_state_nonce_and_pkce(self, client, idp, provider):
        params = start_login(client, idp, provider)
        fake = idp.get(provider)
        assert params["_url"].startswith(fake.metadata["authorization_endpoint"] + "?")
        assert params["response_type"] == "code" and params["client_id"] == fake.client_id
        assert params["redirect_uri"] == f"https://api.connectx.test/api/auth/{provider.value}/callback"
        assert params["scope"] == "openid email profile"
        assert params["code_challenge_method"] == "S256"
        assert len(params["state"]) >= 20 and len(params["nonce"]) >= 20 and len(params["code_challenge"]) >= 40

    def test_each_login_gets_fresh_values(self, client, idp, provider):
        first = start_login(client, idp, provider)
        second = start_login(client, idp, provider)
        assert first["state"] != second["state"] and first["nonce"] != second["nonce"]
        assert first["code_challenge"] != second["code_challenge"]

    def test_not_configured_is_503(self, client, idp, provider, monkeypatch):
        monkeypatch.setattr(settings, "ms_auth_client_secret", "")
        assert client.get(f"/api/auth/{provider.value}/login").status_code == 503
        r = client.get(f"/api/auth/{provider.value}/callback", params={"code": "c", "state": "s"})
        assert landed_with_error(r, "not_configured")

    def test_first_sign_in_creates_a_pending_account_and_a_session(self, client, db, idp, provider):
        r = sign_in(client, idp, provider)
        assert landed_ok(r)
        user = db.scalar(select(User))
        fake = idp.get(provider)
        assert (user.provider, user.provider_subject, user.email) == (provider, fake.subject, "sam@example.test")
        assert user.role == UserRole.PENDING and user.password_hash is None and user.name == "Sam Student"
        assert user.last_login_at is not None
        me = client.get("/api/auth/me").json()
        assert me["role"] == "pending" and me["provider"] == provider.value
        assert client.get("/api/dashboard/status").status_code == 403  # pending lockout
        (cookie,) = session_cookie_headers(r)
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie

    def test_code_is_exchanged_with_the_pkce_verifier_and_client_secret(self, client, idp, provider):
        sign_in(client, idp, provider)
        fake = idp.get(provider)
        (request,) = fake.token_requests
        assert request["grant_type"] == "authorization_code" and request["code"] == "auth-code"
        assert request["redirect_uri"] == f"https://api.connectx.test/api/auth/{provider.value}/callback"
        assert b64url(hashlib.sha256(request["code_verifier"].encode()).digest()) == fake.challenge
        expected_secret = settings.ms_auth_client_secret
        basic = base64.b64decode(fake.auth_headers[0].removeprefix("Basic ")).decode()
        assert basic == f"{fake.client_id}:{expected_secret}" or request.get("client_secret") == expected_secret

    def test_second_sign_in_reuses_the_account_and_keeps_its_role(self, client, app, db, idp, provider):
        assert landed_ok(sign_in(client, idp, provider))
        user = db.scalar(select(User))
        user.role = UserRole.MEMBER
        db.commit()
        again = TestClient(app, follow_redirects=False, headers=CSRF)
        assert landed_ok(sign_in(again, idp, provider))
        assert db.scalar(select(User.id).where(User.id != user.id)) is None
        db.refresh(user)
        assert user.role == UserRole.MEMBER
        assert again.get("/api/dashboard/status").status_code == 200

    def test_changed_email_at_the_provider_does_not_create_a_second_account(self, client, app, db, idp, provider):
        sign_in(client, idp, provider)
        idp.get(provider).email = "renamed@example.test"
        assert landed_ok(sign_in(TestClient(app, follow_redirects=False), idp, provider))
        assert len(db.scalars(select(User)).all()) == 1

    def test_state_mismatch_is_rejected(self, client, db, idp, provider):
        r = sign_in(client, idp, provider, state="forged-state")
        assert landed_with_error(r, "oauth_failed")
        assert db.scalar(select(User)) is None and idp.get(provider).token_requests == []

    def test_callback_without_a_started_login_is_rejected(self, app, db, idp, provider):
        stranger = TestClient(app, follow_redirects=False)
        r = stranger.get(f"/api/auth/{provider.value}/callback", params={"code": "c", "state": "s"})
        assert landed_with_error(r, "oauth_failed")
        assert db.scalar(select(User)) is None

    def test_state_cannot_be_replayed(self, client, db, idp, provider):
        params = start_login(client, idp, provider)
        query = {"code": "auth-code", "state": params["state"]}
        assert landed_ok(client.get(f"/api/auth/{provider.value}/callback", params=query))
        client.cookies.delete(security.SESSION_COOKIE)
        assert landed_with_error(client.get(f"/api/auth/{provider.value}/callback", params=query), "oauth_failed")

    def test_user_denied_at_the_provider(self, client, db, idp, provider):
        params = start_login(client, idp, provider)
        r = client.get(
            f"/api/auth/{provider.value}/callback",
            params={"error": "access_denied", "error_description": "no", "state": params["state"]},
        )
        assert landed_with_error(r, "denied") and db.scalar(select(User)) is None

    @pytest.mark.parametrize(
        ("name", "overrides", "key"),
        [
            ("wrong nonce", {"nonce": "someone-elses-nonce"}, None),
            ("wrong audience", {"aud": "another-app"}, None),
            ("wrong issuer", {"iss": "https://evil.example/v2.0"}, None),
            ("expired", {"exp": 1_000_000_000, "iat": 999_999_000}, None),
            ("no subject", {"sub": None}, None),
            ("forged signature", {}, "other"),
        ],
    )
    def test_invalid_id_tokens_are_rejected(self, client, db, idp, provider, name, overrides, key):
        fake = idp.get(provider)
        fake.overrides = overrides
        if key == "other":
            fake.key = OTHER_KEY
        r = sign_in(client, idp, provider)
        assert landed_with_error(r, "oauth_failed"), name
        assert db.scalar(select(User)) is None

    def test_unusable_profiles_are_rejected(self, client, db, idp, provider):
        idp.get(provider).overrides = {"email": None, "preferred_username": None}
        assert landed_with_error(sign_in(client, idp, provider), "unusable_profile")
        idp.get(provider).overrides = {"email": "not-an-email"}
        assert landed_with_error(sign_in(client, idp, provider), "unusable_profile")
        assert db.scalar(select(User)) is None

    def test_disabled_account_is_refused(self, client, db, idp, provider):
        fake = idp.get(provider)
        add_user(db, fake.email, UserRole.DISABLED, provider=provider, subject=fake.subject)
        assert landed_with_error(sign_in(client, idp, provider), "disabled")

    def test_same_email_with_another_subject_never_takes_over_the_account(self, client, db, idp, provider):
        fake = idp.get(provider)
        owner = add_user(db, fake.email, UserRole.ADMIN, provider=provider, subject="the-real-subject")
        assert landed_with_error(sign_in(client, idp, provider), "account_conflict")
        db.refresh(owner)
        assert owner.provider_subject == "the-real-subject" and len(db.scalars(select(User)).all()) == 1

    def test_provider_claims_can_never_grant_a_role(self, client, db, idp, provider):
        add_user(db, "boss@example.test", UserRole.ADMIN)
        fake = idp.get(provider)
        fake.email = "boss@example.test"
        fake.overrides = {"role": "admin", "roles": ["admin", "Admin"], "groups": ["admins"], "hd": "example.test", "admin": True}
        assert landed_ok(sign_in(client, idp, provider))
        user = db.scalar(select(User).where(User.provider == provider))
        assert user.role == UserRole.PENDING
        assert client.get("/api/admin/users").status_code == 403


class TestMicrosoftSpecifics:
    def test_falls_back_to_preferred_username_when_there_is_no_email_claim(self, client, db, idp):
        idp.microsoft.overrides = {"email": None, "preferred_username": "Sam.Pref@Example.test"}
        assert landed_ok(sign_in(client, idp, AuthProvider.MICROSOFT))
        assert db.scalar(select(User)).email == "sam.pref@example.test"

    @pytest.mark.parametrize("tenant", ["common", "organizations", "consumers"])
    def test_multi_tenant_endpoints_accept_any_tenant_issuer(self, client, db, idp, monkeypatch, tenant):
        monkeypatch.setattr(settings, "ms_auth_tenant", tenant)
        guid = "9188040d-6c67-4c5b-b112-36a304b66dad"
        idp.microsoft.overrides = {"iss": f"https://login.microsoftonline.com/{guid}/v2.0"}
        assert landed_ok(sign_in(client, idp, AuthProvider.MICROSOFT))

    @pytest.mark.parametrize(
        "issuer",
        [
            "https://evil.example/9188040d-6c67-4c5b-b112-36a304b66dad/v2.0",
            "https://login.microsoftonline.com/not-a-guid/v2.0",
            "https://login.microsoftonline.com/9188040d-6c67-4c5b-b112-36a304b66dad/v2.0/extra",
            "https://login.microsoftonline.com.evil.example/9188040d-6c67-4c5b-b112-36a304b66dad/v2.0",
        ],
    )
    def test_multi_tenant_still_rejects_foreign_issuers(self, client, db, idp, monkeypatch, issuer):
        monkeypatch.setattr(settings, "ms_auth_tenant", "common")
        idp.microsoft.overrides = {"iss": issuer}
        assert landed_with_error(sign_in(client, idp, AuthProvider.MICROSOFT), "oauth_failed")
        assert db.scalar(select(User)) is None

    def test_a_single_tenant_app_rejects_other_tenants(self, client, db, idp):
        idp.microsoft.overrides = {"iss": "https://login.microsoftonline.com/9188040d-6c67-4c5b-b112-36a304b66dad/v2.0"}
        assert landed_with_error(sign_in(client, idp, AuthProvider.MICROSOFT), "oauth_failed")


# ─── No linking across providers ───────────────────────────


class TestNoCrossProviderLinking:
    def test_microsoft_sign_in_with_an_admins_email_makes_a_separate_pending_account(self, client, db, idp):
        admin = add_user(db, "sam@example.test", UserRole.ADMIN)
        assert landed_ok(sign_in(client, idp, AuthProvider.MICROSOFT))
        db.refresh(admin)
        assert admin.role == UserRole.ADMIN and admin.provider == AuthProvider.PASSWORD and admin.provider_subject is None
        microsoft = db.scalar(select(User).where(User.provider == AuthProvider.MICROSOFT))
        assert microsoft.id != admin.id and microsoft.role == UserRole.PENDING and microsoft.email == admin.email
        assert client.get("/api/auth/me").json()["id"] == microsoft.id
        assert client.get("/api/admin/users").status_code == 403

    def test_microsoft_sign_in_does_not_link_to_a_google_account(self, client, db, idp):
        google = add_user(db, "sam@example.test", UserRole.MEMBER, provider=AuthProvider.GOOGLE, subject="g-1")
        assert landed_ok(sign_in(client, idp, AuthProvider.MICROSOFT))
        microsoft = db.scalar(select(User).where(User.provider == AuthProvider.MICROSOFT))
        assert microsoft.id != google.id and microsoft.role == UserRole.PENDING
        db.refresh(google)
        assert google.role == UserRole.MEMBER and google.provider_subject == "g-1"

    def test_registering_a_password_for_an_oauth_email_makes_a_separate_account(self, client, db):
        google = add_user(db, "sam@example.test", UserRole.ADMIN, provider=AuthProvider.GOOGLE, subject="g-1")
        r = client.post("/api/auth/register", json={"email": "sam@example.test", "name": "Sam", "password": PASSWORD})
        assert r.status_code == 202
        password_account = db.scalar(select(User).where(User.provider == AuthProvider.PASSWORD))
        assert password_account.id != google.id and password_account.role == UserRole.PENDING
        db.refresh(google)
        assert google.role == UserRole.ADMIN and google.password_hash is None

    def test_password_login_cannot_reach_an_oauth_account(self, client, db):
        add_user(db, "sam@example.test", UserRole.ADMIN, provider=AuthProvider.GOOGLE, subject="g-1")
        r = client.post("/api/auth/login", json={"email": "sam@example.test", "password": PASSWORD})
        assert r.status_code == 401

    def test_password_and_microsoft_can_share_one_email(self, client, db, idp):
        add_user(db, "sam@example.test", UserRole.MEMBER)
        sign_in(client, idp, AuthProvider.MICROSOFT)
        users = db.scalars(select(User).order_by(User.id)).all()
        assert {u.provider for u in users} == {AuthProvider.PASSWORD, AuthProvider.MICROSOFT} and {u.email for u in users} == {"sam@example.test"}


class TestSchema:
    def test_uniqueness_is_per_provider(self, db):
        add_user(db, "a@example.test", provider=AuthProvider.GOOGLE, subject="s-1")
        add_user(db, "a@example.test", provider=AuthProvider.MICROSOFT, subject="s-1")  # same email, same subject, other provider
        add_user(db, "a@example.test")  # password
        add_user(db, "b@example.test")  # a second password account: subject is NULL for both
        for duplicate in (
            dict(email="a@example.test", provider=AuthProvider.GOOGLE, subject="s-2"),  # same (provider, email)
            dict(email="c@example.test", provider=AuthProvider.GOOGLE, subject="s-1"),  # same (provider, subject)
            dict(email="a@example.test", provider=AuthProvider.PASSWORD, subject=None),  # same (provider, email)
        ):
            with pytest.raises(IntegrityError):
                add_user(db, **duplicate)
            db.rollback()

    def test_new_users_default_to_pending(self, db):
        db.add(User(email="x@example.test", name="X", provider=AuthProvider.PASSWORD, password_hash="h"))
        db.commit()
        assert db.scalar(select(User)).role == UserRole.PENDING

    def test_migration_chain(self):
        script = ScriptDirectory(str(Path(app_db_package.__file__).parent / "alembic"))
        assert script.get_heads() == ["009_message_blocked"]
        assert script.get_revision("006_users").down_revision == "005_inquiries"


# ─── create-admin CLI ──────────────────────────────────────


def prompts(*answers):
    queue = iter(answers)
    return lambda _label: next(queue)


class TestCli:
    def test_creates_an_admin_with_a_hashed_password(self, db, capsys):
        code = cli.main(["create-admin", "--email", " Boss@Example.test "], prompt=prompts("a long password", "a long password"))
        assert code == 0 and "Created admin boss@example.test" in capsys.readouterr().out
        user = db.scalar(select(User))
        assert (user.email, user.role, user.provider) == ("boss@example.test", UserRole.ADMIN, AuthProvider.PASSWORD)
        assert user.name == "boss" and security.verify_password(user.password_hash, "a long password")
        assert "a long password" not in user.password_hash

    def test_the_new_admin_can_sign_in(self, app, db):
        cli.main(["create-admin", "--email", "boss@example.test", "--name", "The Boss"], prompt=prompts("a long password", "a long password"))
        r = TestClient(app, headers=CSRF).post("/api/auth/login", json={"email": "boss@example.test", "password": "a long password"})
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin" and r.json()["user"]["name"] == "The Boss"

    @pytest.mark.parametrize(
        ("email", "answers", "message"),
        [
            ("not-an-email", ("a long password", "a long password"), "valid email"),
            ("boss@example.test", ("short", "short"), "10 to 128"),
            ("boss@example.test", ("a long password", "another long one"), "do not match"),
        ],
    )
    def test_bad_input_creates_nothing(self, db, capsys, email, answers, message):
        assert cli.main(["create-admin", "--email", email], prompt=prompts(*answers)) == 1
        assert message in capsys.readouterr().err
        assert db.scalar(select(User)) is None

    def test_existing_password_account_becomes_admin_with_the_new_password(self, db):
        user = add_user(db, "boss@example.test", UserRole.PENDING)
        cli.main(["create-admin", "--email", "boss@example.test"], prompt=prompts("a fresh password", "a fresh password"))
        db.refresh(user)
        assert user.role == UserRole.ADMIN and security.verify_password(user.password_hash, "a fresh password")
        assert len(db.scalars(select(User)).all()) == 1

    def test_an_oauth_account_with_the_same_email_is_left_alone(self, db):
        google = add_user(db, "boss@example.test", UserRole.MEMBER, provider=AuthProvider.GOOGLE, subject="g-1")
        cli.main(["create-admin", "--email", "boss@example.test"], prompt=prompts("a fresh password", "a fresh password"))
        db.refresh(google)
        assert google.role == UserRole.MEMBER and len(db.scalars(select(User)).all()) == 2

    def test_password_is_never_taken_from_the_command_line(self):
        with pytest.raises(SystemExit):
            cli.main(["create-admin", "--email", "boss@example.test", "--password", "x"])
