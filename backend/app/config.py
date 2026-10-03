from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent.parent

# The test suite sets CONNECTX_IGNORE_ENV_FILE so a developer's real .env is never read.
ENV_FILE: str | None = None if os.environ.get("CONNECTX_IGNORE_ENV_FILE") else str(_ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_env: str = "development"
    dry_run: bool = True
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    backend_reload: bool = True

    # Frontend
    frontend_port: int = 5173

    # LLM (free tiers only) — Groq (primary), Gemini (secondary), Ollama Cloud (fallback)
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash-exp"
    ollama_url: str = "https://ollama.com"
    ollama_api_key: str = ""
    ollama_model: str = "llama3.2"

    # LinkedIn (persistent Playwright profile; manual first login, no password stored)
    linkedin_profile_dir: str = "/app/secrets/linkedin-profile"
    linkedin_headless: bool = True

    # Gmail / Email
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_sender_email: str = ""

    # WhatsApp (owner notifications and message review)
    whatsapp_phone_number_id: str = ""
    whatsapp_token: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_owner_number: str = ""
    hold_window_minutes: int = 10

    # Training & Placement Officer details shared with interested companies
    tpo_name: str = ""
    tpo_phone: str = ""
    tpo_email: str = ""

    # Dashboard auth: session JWT (JWT_SECRET must be 16+ characters or every login is refused)
    jwt_secret: str = ""
    jwt_expire_hours: int = 12

    # Sign in with Google / Microsoft (OpenID Connect). Separate from MS_CLIENT_ID, which sends Outlook mail.
    google_client_id: str = ""
    google_client_secret: str = ""
    ms_auth_client_id: str = ""
    ms_auth_client_secret: str = ""
    ms_auth_tenant: str = "common"
    # Public origins: BACKEND_URL builds the OAuth redirect URIs, FRONTEND_URL is where users land afterwards.
    backend_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:5173"

    # Database
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "connectx"
    postgres_user: str = "connectx"
    postgres_password: str = ""

    # Redis (APScheduler backing)
    redis_url: str = "redis://redis:6379/0"

    # Search
    searxng_url: str = "http://searxng:8080"

    # Guardrails
    linkedin_daily_cap: int = 20
    email_daily_cap: int = 40
    send_window_start: str = "10:00"
    send_window_end: str = "17:00"
    send_window_tz: str = "Asia/Kolkata"
    min_touch_interval_days: int = 14
    daily_cap_reset_hour: int = 0

    # gspread
    google_service_account_file: str = "/app/creds/gspread-service-account.json"
    google_sheet_id: str = ""

    @cached_property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
