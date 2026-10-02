from __future__ import annotations

from fastapi import FastAPI

from app.api.main import api_router

app = FastAPI(
    title="AI ConnectX Outreach Agent",
    description="University placement outreach automation (DRY_RUN by default).",
    version="0.1.0",
)

app.include_router(api_router)
