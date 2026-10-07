"""`python -m app.cli doctor`: one table of OK / FAIL / SKIPPED. Never prints a secret, only setting NAMES."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.channel_config import missing_settings, unregistered_jobs
from app.config import settings
from app.redact import redact

OK, FAIL, SKIPPED = "OK", "FAIL", "SKIPPED"
Row = tuple[str, str, str]

LLM_PROVIDERS = (
    ("Groq", "GROQ_API_KEY", "groq_api_key", "groq_model"),
    ("Gemini", "GEMINI_API_KEY", "gemini_api_key", "gemini_model"),
    ("Ollama", "OLLAMA_API_KEY", "ollama_api_key", "ollama_model"),
)


def _error(exc: Exception) -> str:
    return redact(f"{type(exc).__name__}: {exc}").splitlines()[0][:200]


def check_database() -> Row:
    from sqlalchemy import text

    from app import db

    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        return "Database", FAIL, _error(exc)
    return "Database", OK, "connected"


def check_migrations() -> Row:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app import db

    try:
        ini = Path(db.__file__).parent / "alembic.ini"
        head = ScriptDirectory.from_config(Config(str(ini))).get_current_head()
        with db.engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    except Exception as exc:
        return "Migrations", FAIL, _error(exc)
    if current == head:
        return "Migrations", OK, f"at head ({head})"
    return "Migrations", FAIL, f"at {current or 'nothing'}, head is {head}: cd backend; alembic -c app/db/alembic.ini upgrade head"


def check_job_store() -> Row:
    from sqlalchemy import inspect

    from app import db

    try:
        has_table = inspect(db.engine).has_table("apscheduler_jobs")
    except Exception as exc:
        return "Job store", FAIL, _error(exc)
    if not has_table:
        return "Job store", SKIPPED, "reachable; apscheduler_jobs is created when the scheduler first starts"
    return "Job store", OK, "reachable (apscheduler_jobs)"


def check_channel(channel: str) -> Row:
    label = channel.capitalize() if channel != "linkedin" else "LinkedIn"
    missing = missing_settings(channel)
    if missing:
        return label, SKIPPED, f"missing {', '.join(missing)}"
    if channel == "linkedin" and not settings.linkedin_enabled:
        return label, SKIPPED, "disabled (LINKEDIN_ENABLED=false)"
    return label, OK, "enabled"


def check_scheduler() -> Row:
    from app.scheduler import build_specs

    registered = [spec.id for spec in build_specs()]
    skipped = unregistered_jobs()
    detail = f"{len(registered)} jobs registered"
    if skipped:
        detail += "; not registered: " + ", ".join(f"{job} ({channel} settings missing)" for job, channel in skipped.items())
    return "Scheduler jobs", OK, detail


def last_models() -> dict[str, str]:
    """Model each provider last answered with (saved by the LLM client); empty when the database is down."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.db.models import Setting
    from app.llm.client import MODEL_USED_PREFIX

    try:
        with SessionLocal() as db:
            rows = db.scalars(select(Setting).where(Setting.key.like(MODEL_USED_PREFIX + "%")))
            return {row.key[len(MODEL_USED_PREFIX):]: row.value for row in rows}
    except Exception:
        return {}


def check_llm() -> list[Row]:
    rows: list[Row] = []
    used = last_models()
    for name, env_name, key_attr, model_attr in LLM_PROVIDERS:
        if getattr(settings, key_attr):
            provider = name.lower()
            detail = f"key set, model {getattr(settings, model_attr)}"
            fallback = getattr(settings, f"{provider}_model_fallback", "")
            if fallback:
                detail += f", fallback {fallback}"
            if provider in used:
                detail += f", last used {used[provider]}"
            rows.append((f"LLM {name}", OK, detail))
        else:
            rows.append((f"LLM {name}", SKIPPED, f"no key ({env_name})"))
    if all(status == SKIPPED for _, status, _ in rows):
        rows.append(("LLM", FAIL, "no provider has a key"))
    return rows


def collect() -> list[Row]:
    from app import sandbox

    rows = [check_database()]
    database_ok = rows[0][1] == OK
    rows.append(check_migrations() if database_ok else ("Migrations", SKIPPED, "database not reachable"))
    rows.extend(check_channel(channel) for channel in ("outlook", "whatsapp", "linkedin"))
    rows.append(("DRY_RUN", OK, "true (nothing is sent)" if settings.dry_run else "false (LIVE: messages are sent)"))
    entries = sandbox.allowlist()
    rows.append(
        ("SEND_ALLOWLIST", OK, f"active ({len(entries)} address(es))")
        if entries
        else ("SEND_ALLOWLIST", SKIPPED, "inactive (email may go to any address)")
    )
    rows.append(check_job_store() if database_ok else ("Job store", SKIPPED, "database not reachable"))
    rows.append(check_scheduler())
    rows.extend(check_llm())
    return rows


def run_doctor(out: Callable[[str], None] = print) -> int:
    rows = collect()
    width = max(len(name) for name, _, _ in rows)
    for name, status, detail in rows:
        out(f"{name:<{width}}  {status:<7}  {detail}")
    failed = [name for name, status, _ in rows if status == FAIL]
    out(f"\n{len(failed)} failed" if failed else "\nNo failures")
    return 1 if failed else 0
