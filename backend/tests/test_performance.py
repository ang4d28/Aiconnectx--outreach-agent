"""Request timing, the connection pool, indexes and the "no password hashing per request" rule."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import db as app_db
from app.api.main import api_router
from app.auth import oauth, security
from app.db import get_session
from app.db.models import AttentionItem, AttentionType, AuthProvider, Base, User, UserRole
from app.timing import TimingMiddleware

CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}
MIGRATION = Path(__file__).resolve().parent.parent / "app" / "db" / "alembic" / "versions" / "010_perf_indexes.py"


# ─── Timing middleware ─────────────────────────────────────


@pytest.fixture
def timed_app(db):
    app = FastAPI()
    app.add_middleware(TimingMiddleware, slow_ms=40)

    @app.get("/fast")
    def fast(session: Session = Depends(get_session)):
        for _ in range(3):
            session.execute(text("select 1"))
        return {"ok": True}

    @app.get("/slow")
    def slow():
        time.sleep(0.08)
        return {"ok": True}

    app.dependency_overrides[get_session] = lambda: db
    return app


class TestTimingMiddleware:
    def test_fast_request_is_logged_with_method_path_status_and_query_count(self, timed_app, caplog):
        caplog.set_level(logging.INFO, logger="app.timing")
        assert TestClient(timed_app).get("/fast?secret=abc").status_code == 200
        [record] = [r for r in caplog.records if r.name == "app.timing"]
        assert record.levelno == logging.INFO
        assert re.fullmatch(r"GET /fast 200 \d+ ms, 3 queries", record.getMessage())
        assert "secret" not in record.getMessage()  # the query string is never logged

    def test_requests_over_the_threshold_are_warnings(self, timed_app, caplog):
        caplog.set_level(logging.INFO, logger="app.timing")
        TestClient(timed_app).get("/slow")
        [record] = [r for r in caplog.records if r.name == "app.timing"]
        assert record.levelno == logging.WARNING
        assert record.getMessage().startswith("SLOW GET /slow 200 ") and "0 queries" in record.getMessage()

    def test_query_counts_do_not_leak_between_requests(self, timed_app, caplog):
        caplog.set_level(logging.INFO, logger="app.timing")
        http = TestClient(timed_app)
        http.get("/fast")
        http.get("/fast")
        counts = [re.search(r"(\d+) queries", r.getMessage())[1] for r in caplog.records if r.name == "app.timing"]
        assert counts == ["3", "3"]

    def test_the_real_app_has_the_middleware_installed(self):
        from app.main import app

        assert any(m.cls is TimingMiddleware for m in app.user_middleware)


# ─── Connection pool ───────────────────────────────────────


class TestPool:
    def test_postgres_engine_pool_settings(self):
        engine = app_db.build_engine("postgresql+psycopg2://user:pw@db.invalid:5432/name")
        assert 5 <= engine.pool.size() <= 10
        assert engine.pool._max_overflow == 4
        assert engine.pool._pre_ping is True
        assert engine.pool._recycle == 1800
        assert engine.pool._timeout == 10  # fail fast instead of queueing forever when the pool is empty


# ─── Indexes ───────────────────────────────────────────────


class TestIndexes:
    def model_indexes(self):
        return {
            (t.name, ix.name, tuple(c.name for c in ix.columns))
            for t in Base.metadata.tables.values()
            for ix in t.indexes
        }

    def test_filter_and_sort_columns_are_indexed(self):
        by_table: dict[str, set[str]] = {}
        for table, _name, columns in self.model_indexes():
            by_table.setdefault(table, set()).update(columns)
        assert {"state", "channel", "created_at", "contact_id", "post_id"} <= by_table["outreaches"]
        assert {"outreach_id", "direction", "status", "sent_at"} <= by_table["messages"]
        assert {"status", "type", "created_at"} <= by_table["attention_items"]
        assert {"status", "created_at"} <= by_table["inquiries"]
        assert {"role"} <= by_table["users"]
        assert {"company_id"} <= by_table["contacts"]

    def test_the_migration_creates_exactly_the_indexes_declared_on_the_models(self):
        source = MIGRATION.read_text(encoding="utf-8")
        upgrade = source[source.index("def upgrade"): source.index("def downgrade")]
        created = {
            (table, name, tuple(c.strip().strip('"') for c in cols.split(",")))
            for name, table, cols in re.findall(r'create_index\("(\w+)", "(\w+)", \[([^\]]*)\]', upgrade)
        }
        # draft_versions is a later table: its index is created by 012_draft_versions, not by this migration.
        assert created == {ix for ix in self.model_indexes() if ix[0] != "draft_versions"}
        downgrade = source[source.index("def downgrade"):]
        assert set(re.findall(r'drop_index\("(\w+)"', downgrade)) == {name for _t, name, _c in created}

    def test_indexes_exist_in_the_database_schema(self, db):
        names = {row[0] for row in db.execute(text("select name from sqlite_master where type = 'index'"))}
        assert {"ix_outreaches_state", "ix_messages_direction_status", "ix_attention_items_status_type"} <= names


# ─── Password hashing happens on login and registration only ──


@pytest.fixture
def app(db):
    application = FastAPI()
    oauth.install_session_middleware(application)
    application.include_router(api_router)
    application.dependency_overrides[get_session] = lambda: db
    return application


def signed_in(app, db, role: UserRole) -> TestClient:
    user = User(
        email=f"{role.value}@example.test", name=role.value, role=role, provider=AuthProvider.PASSWORD,
        password_hash=security.hash_password("correct horse battery"),
    )
    db.add(user)
    db.commit()
    token, _ = security.create_session_token(user)
    http = TestClient(app, headers=CSRF)
    http.cookies.set(security.SESSION_COOKIE, token)
    return http


class TestNoHashingPerRequest:
    def test_authenticated_requests_never_touch_argon2(self, app, db, monkeypatch):
        from argon2 import PasswordHasher

        monkeypatch.setattr(security, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
        security._dummy_hash.cache_clear()
        http = signed_in(app, db, UserRole.ADMIN)

        class Forbidden:
            def __getattr__(self, name):
                raise AssertionError(f"argon2 {name}() called while serving an authenticated request")

        monkeypatch.setattr(security, "_hasher", Forbidden())
        for path in ("/api/auth/me", "/api/dashboard/status", "/api/dashboard/badges", "/api/dashboard/overview",
                     "/api/dashboard/leads", "/api/channels/status", "/api/admin/users"):
            assert http.get(path).status_code == 200, path
        security._dummy_hash.cache_clear()

    def test_default_hasher_uses_argon2id_with_moderate_cost(self):
        from argon2 import Type

        hasher = security._hasher
        assert hasher.type == Type.ID
        assert hasher.memory_cost <= 128 * 1024 and hasher.time_cost <= 4  # tens of ms, not seconds


# ─── Every list endpoint is bounded ────────────────────────


class TestPaginatedLists:
    def test_legacy_attention_list_is_bounded(self, app, db):
        db.add_all(AttentionItem(type=AttentionType.NEEDS_MANUAL, payload={}) for _ in range(5))
        db.commit()
        http = signed_in(app, db, UserRole.VIEWER)
        assert len(http.get("/api/attention").json()) == 5
        assert [i["id"] for i in http.get("/api/attention?limit=2").json()] == [5, 4]
        assert [i["id"] for i in http.get("/api/attention?limit=2&skip=2").json()] == [3, 2]
        assert http.get("/api/attention?limit=201").status_code == 422
        assert http.get("/api/attention?skip=-1").status_code == 422
