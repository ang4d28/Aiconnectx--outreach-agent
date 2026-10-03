"""Settings the owner can change from the dashboard without redeploying.

Overrides live in the settings table under "cfg:<name>" and are applied on top
of the environment values. The scheduler re-applies them before every job and
the API applies them at start-up and on every save, so both processes follow.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from app.config import settings
from app.db.models import Setting

logger = logging.getLogger(__name__)

PREFIX = "cfg:"
SEARCH_QUERIES_KEY = "search_queries"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE = re.compile(r"^[0-9+()\-\s]{6,30}$")


def _int_in(low: int, high: int) -> Callable[[Any], int]:
    def parse(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("must be a whole number")
        number = int(value)
        if not low <= number <= high:
            raise ValueError(f"must be between {low} and {high}")
        return number

    return parse


def _hhmm(value: Any) -> str:
    if not isinstance(value, str) or not HHMM.match(value):
        raise ValueError("must be a time like 10:00")
    return value


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise ValueError("must be true or false")


def _text(max_length: int, pattern: re.Pattern[str] | None = None) -> Callable[[Any], str]:
    def parse(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be text")
        value = value.strip()
        if len(value) > max_length:
            raise ValueError(f"must be at most {max_length} characters")
        if value and pattern and not pattern.match(value):
            raise ValueError("has an invalid format")
        return value

    return parse


EDITABLE: dict[str, Callable[[Any], Any]] = {
    "linkedin_daily_cap": _int_in(1, 100),
    "email_daily_cap": _int_in(1, 200),
    "send_window_start": _hhmm,
    "send_window_end": _hhmm,
    "hold_window_minutes": _int_in(1, 240),
    "dry_run": _bool,
    "tpo_name": _text(120),
    "tpo_phone": _text(30, PHONE),
    "tpo_email": _text(254, EMAIL),
}


def validate_changes(changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Parse the submitted values. Returns (clean values, errors by field)."""
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, raw in changes.items():
        parser = EDITABLE.get(name)
        if parser is None:
            errors[name] = "is not an editable setting"
            continue
        try:
            clean[name] = parser(raw)
        except (ValueError, TypeError) as exc:
            errors[name] = str(exc)
    start = clean.get("send_window_start", settings.send_window_start)
    end = clean.get("send_window_end", settings.send_window_end)
    if ("send_window_start" in clean or "send_window_end" in clean) and start >= end:
        errors["send_window_end"] = "must be later than the start"
    return clean, errors


def current_values() -> dict[str, Any]:
    return {name: getattr(settings, name) for name in EDITABLE}


def save_overrides(db: Any, values: dict[str, Any]) -> None:
    for name, value in values.items():
        key = PREFIX + name
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=text))
        else:
            row.value = text
    db.commit()
    for name, value in values.items():
        setattr(settings, name, value)


def apply_overrides(db: Any) -> list[str]:
    """Load saved overrides into the live settings object. Bad rows are skipped."""
    applied: list[str] = []
    for name, parser in EDITABLE.items():
        row = db.get(Setting, PREFIX + name)
        if row is None:
            continue
        try:
            setattr(settings, name, parser(row.value))
            applied.append(name)
        except (ValueError, TypeError):
            logger.warning(f"Ignoring invalid saved setting {name}={row.value!r}")
    return applied
