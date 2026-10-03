from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.admin_users import router as admin_users_router
from app.api.attention import router as attention_router
from app.api.auth import router as auth_router
from app.auth.deps import authorize
from app.api.dashboard import router as dashboard_router
from app.api.drafts import router as drafts_router
from app.api.routes.companies import router as companies_router
from app.api.routes.control import router as control_router
from app.api.routes.outreach import router as outreach_router
from app.api.routes.persons import router as persons_router
from app.api.export import router as export_router
from app.api.inquiries import router as inquiries_router
from app.api.reports import router as reports_router
from app.api.routes.tracking import router as tracking_router
from app.api.whatsapp_webhook import router as whatsapp_router

api_router = APIRouter(prefix="/api")

# Public: sign-in and registration, website contact form, WhatsApp webhook (signature-verified), health.
api_router.include_router(auth_router)
api_router.include_router(inquiries_router)
api_router.include_router(whatsapp_router)

# Everything else needs a signed-in, approved user; `authorize` decides by role (see app.auth.deps).
protected = [Depends(authorize)]
for router in (
    admin_users_router,
    attention_router,
    companies_router,
    dashboard_router,
    drafts_router,
    export_router,
    control_router,
    outreach_router,
    persons_router,
    reports_router,
    tracking_router,
):
    api_router.include_router(router, dependencies=protected)


@api_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
