from __future__ import annotations

from fastapi import APIRouter

from app.api.drafts import router as drafts_router
from app.api.routes.companies import router as companies_router
from app.api.routes.control import router as control_router
from app.api.routes.outreach import router as outreach_router
from app.api.routes.persons import router as persons_router
from app.api.routes.tracking import router as tracking_router

api_router = APIRouter(prefix="/api")

api_router.include_router(companies_router)
api_router.include_router(drafts_router)
api_router.include_router(control_router)
api_router.include_router(outreach_router)
api_router.include_router(persons_router)
api_router.include_router(tracking_router)


@api_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
