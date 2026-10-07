from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

POOL_SIZE = 8
POOL_MAX_OVERFLOW = 4


def build_engine(url: str) -> Engine:
    """One engine per process (the API and the scheduler each have their own).

    A request holds one connection for its duration; 8 + 4 overflow per process stays well under Postgres'
    default of 100 connections. pool_pre_ping replaces a connection that Postgres closed while idle instead of
    failing the request; pool_recycle retires connections before a proxy or firewall idle timeout cuts them.
    """
    return create_engine(
        url,
        echo=False,
        pool_size=POOL_SIZE,
        max_overflow=POOL_MAX_OVERFLOW,
        pool_timeout=10,
        pool_recycle=1800,
        pool_pre_ping=True,
    )


engine = build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session() -> Session:
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
