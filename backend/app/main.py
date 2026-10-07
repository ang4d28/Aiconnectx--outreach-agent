from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import redact
from app.api.main import api_router
from app.api.whatsapp_webhook import set_handler
from app.auth.oauth import install_session_middleware
from app.config import warn_env_overrides
from app.sandbox import startup_line
from app.timing import TimingMiddleware, configure_logging
from app.workers.review import ReviewCommands


def apply_saved_settings() -> None:
    """Load settings saved from the dashboard; the API still starts if the database is not ready."""
    try:
        from app.db import SessionLocal
        from app.runtime_config import apply_overrides

        db = SessionLocal()
        try:
            apply_overrides(db)
        finally:
            db.close()
    except Exception as exc:
        logging.getLogger(__name__).error(f"Could not load dashboard settings: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    redact.install()
    warn_env_overrides()
    set_handler(ReviewCommands())
    apply_saved_settings()
    logging.getLogger(__name__).info(startup_line())
    yield


app = FastAPI(
    title="AI ConnectX Outreach Agent",
    description="University placement outreach automation (DRY_RUN by default).",
    version="0.1.0",
    lifespan=lifespan,
)

install_session_middleware(app)  # holds the Microsoft sign-in state between redirects
app.add_middleware(TimingMiddleware)  # outermost: times everything below it
app.include_router(api_router)
