from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import repository
from app.db import get_session
from app.db.schemas import SettingCreate, SettingRead, SettingUpdate

router = APIRouter(tags=["control"])


@router.get("/config", response_model=dict)
def get_config() -> dict:
    """Return non-secret runtime configuration."""
    return {
        "dry_run": settings.dry_run,
        "app_env": settings.app_env,
        "backend_host": settings.backend_host,
        "backend_port": settings.backend_port,
        "linkedin_daily_cap": settings.linkedin_daily_cap,
        "email_daily_cap": settings.email_daily_cap,
        "send_window_start": settings.send_window_start,
        "send_window_end": settings.send_window_end,
        "send_window_tz": settings.send_window_tz,
        "min_touch_interval_days": settings.min_touch_interval_days,
        "daily_cap_reset_hour": settings.daily_cap_reset_hour,
    }


@router.get("/guardrails", response_model=dict)
def get_guardrails() -> dict:
    """Return current guardrail settings."""
    return {
        "linkedin_daily_cap": settings.linkedin_daily_cap,
        "email_daily_cap": settings.email_daily_cap,
        "send_window_start": settings.send_window_start,
        "send_window_end": settings.send_window_end,
        "send_window_tz": settings.send_window_tz,
        "min_touch_interval_days": settings.min_touch_interval_days,
        "daily_cap_reset_hour": settings.daily_cap_reset_hour,
    }


@router.get("/settings", response_model=list[SettingRead])
def list_settings(
    db: Session = Depends(get_session),
) -> list[SettingRead]:
    return repository.get_settings(db)


@router.get("/settings/{key}", response_model=SettingRead)
def get_setting(
    key: str,
    db: Session = Depends(get_session),
) -> SettingRead:
    setting = repository.get_setting(db, key)
    if not setting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Setting not found")
    return setting


@router.post("/settings", response_model=SettingRead, status_code=status.HTTP_201_CREATED)
def create_setting(
    data: SettingCreate,
    db: Session = Depends(get_session),
) -> SettingRead:
    existing = repository.get_setting(db, data.key)
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Setting with this key already exists"
        )
    return repository.create_setting(db, data)


@router.patch("/settings/{key}", response_model=SettingRead)
def update_setting(
    key: str,
    data: SettingUpdate,
    db: Session = Depends(get_session),
) -> SettingRead:
    db_setting = repository.get_setting(db, key)
    if not db_setting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Setting not found")
    return repository.update_setting(db, db_setting, data)


@router.delete("/settings/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_setting(
    key: str,
    db: Session = Depends(get_session),
) -> None:
    db_setting = repository.get_setting(db, key)
    if not db_setting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Setting not found")
    repository.delete_setting(db, key)
