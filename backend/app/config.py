from __future__ import annotations

import logging
import os
from functools import cached_property
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent.parent

# The test suite sets CONNECTX_IGNORE_ENV_FILE so a developer's real .env is never read.
ENV_FILE: str | None = None if os.environ.get("CONNECTX_IGNORE_ENV_FILE") else str(_ROOT / ".env")


def resolve_path(value: str, *, is_dir: bool = False) -> str:
    """Resolve a path setting against the project root unless it is absolute, and create its folder.

    A folder that cannot be created is left for the first use to report.
    """
    if not value:
        return value
    path = Path(value)
    if not path.is_absolute():
        path = _ROOT / path
    try:
        (path if is_dir else path.parent).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.getLogger(__name__).warning(f"Could not create folder for {path}: {exc}")
    return str(path)


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

    # LinkedIn (persistent Playwright profile; manual first login, no password stored).
    # LINKEDIN_ENABLED=false keeps the channel off whatever the session state.
    linkedin_enabled: bool = True
    linkedin_profile_dir: str = "secrets/linkedin-profile"
    linkedin_headless: bool = True

    # Outlook (Microsoft Graph) is the only email channel
    ms_token_cache_path: str = "secrets/ms_token.json"

    # Sandbox: comma-separated email addresses. When set, email goes only to these (blocked by sandbox otherwise).
    send_allowlist: str = ""

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

    # Sign in with Microsoft (OpenID Connect). Separate from MS_CLIENT_ID, which sends Outlook mail.
    ms_auth_client_id: str = ""
    ms_auth_client_secret: str = ""
    ms_auth_tenant: str = "common"
    # Public origins: BACKEND_URL builds the OAuth redirect URIs, FRONTEND_URL is where users land afterwards.
    backend_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:5173"

    # Database
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "connectx"
    postgres_user: str = "connectx"
    postgres_password: str = ""

    # Guardrails
    linkedin_daily_cap: int = 20
    email_daily_cap: int = 40
    send_window_start: str = "10:00"
    send_window_end: str = "17:00"
    send_window_tz: str = "Asia/Kolkata"
    min_touch_interval_days: int = 14
    daily_cap_reset_hour: int = 0

    # gspread
    google_service_account_file: str = "secrets/gspread-service-account.json"
    google_sheet_id: str = ""

    @field_validator("linkedin_profile_dir")
    @classmethod
    def _profile_dir(cls, value: str) -> str:
        return resolve_path(value, is_dir=True)

    @field_validator("ms_token_cache_path", "google_service_account_file")
    @classmethod
    def _file_path(cls, value: str) -> str:
        return resolve_path(value)

    @cached_property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
