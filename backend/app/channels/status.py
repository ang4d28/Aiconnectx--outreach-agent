"""Channel connection status for the dashboard: LinkedIn, Outlook and WhatsApp.

Only status is stored, one settings row per channel (channel_status:<channel>, JSON):
state (connected | needs_login | disabled | error), account_name, profile_url, last_checked_at
and a short detail. Never a password, token or cookie value.

The state shown combines the last check with what is configured right now: LINKEDIN_ENABLED=false
and the <channel>_disabled flags win over the last check, a channel without credentials is an
error, and "unknown" means it has not been checked yet.

When a LinkedIn check finds needs_login, every LinkedIn action stops (linkedin_disabled=true with
reason needs_login, no global pause), a "Reconnect LinkedIn" attention item is opened and the owner
gets a WhatsApp alert. A later connected check (CLI login, dashboard Connect or Check now) lifts that
stop and resolves the item. A stop for another reason (captcha, repeated errors) stays until an
admin re-enables the channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels import linkedin_session
from app.channels.linkedin import DISABLED_KEY as LINKEDIN_DISABLED_KEY
from app.channels.linkedin import DISABLED_REASON_KEY as LINKEDIN_REASON_KEY
from app.channels.linkedin_session import CONNECTED, DISABLED, ERROR, NEEDS_LOGIN, SessionStatus
from app.config import settings
from app.db.models import AttentionItem, AttentionStatus, AttentionType, Setting
from app.workers.send_queue import EMAIL_DISABLED_KEY, as_utc, is_truthy, utc_now

logger = logging.getLogger(__name__)

CHANNELS = ("linkedin", "outlook", "whatsapp")
LABELS = {"linkedin": "LinkedIn", "outlook": "Outlook", "whatsapp": "WhatsApp"}
UNKNOWN = "unknown"
STATUS_PREFIX = "channel_status:"
NEEDS_LOGIN_REASON = "needs_login"
RECONNECT_TITLE = "Reconnect LinkedIn"
# Before a LinkedIn work batch a check younger than this is trusted.
PREFLIGHT_MAX_AGE = timedelta(minutes=30)
TOKEN_EXPIRED_CODE = 190  # Meta Graph OAuthException: the access token is invalid or expired

Checker = Callable[[], Awaitable[SessionStatus]]
Notify = Callable[[str], Awaitable[Any]]


# ─── Stored status ─────────────────────────────────────────


def _get(db: Session, key: str) -> str | None:
    row = db.get(Setting, key)
    return row.value if row else None


def _put(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def _parse(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def load_status(db: Session, channel: str) -> dict[str, Any] | None:
    return _parse(_get(db, STATUS_PREFIX + channel))


# Every settings row the status rows depend on, read together in one query.
SNAPSHOT_KEYS = (
    *(STATUS_PREFIX + channel for channel in CHANNELS),
    LINKEDIN_DISABLED_KEY,
    LINKEDIN_REASON_KEY,
    EMAIL_DISABLED_KEY,
)


def read_snapshot(db: Session, extra_keys: tuple[str, ...] = ()) -> dict[str, str]:
    keys = (*SNAPSHOT_KEYS, *extra_keys)
    return {row.key: row.value for row in db.scalars(select(Setting).where(Setting.key.in_(keys)))}


def save_status(db: Session, channel: str, status: SessionStatus, now: datetime) -> dict[str, Any]:
    previous = load_status(db, channel) or {}
    data = {
        "state": status.state,
        "account_name": status.account_name,
        "profile_url": status.profile_url,
        "detail": status.detail,
        "last_checked_at": as_utc(now).isoformat(),
    }
    if status.state == ERROR:  # a failed check says nothing new about the account
        data["account_name"] = data["account_name"] or previous.get("account_name")
        data["profile_url"] = data["profile_url"] or previous.get("profile_url")
    _put(db, STATUS_PREFIX + channel, json.dumps(data))
    return data


def _last_checked(stored: dict[str, Any] | None) -> datetime | None:
    try:
        return as_utc(datetime.fromisoformat(stored["last_checked_at"])) if stored else None
    except (KeyError, TypeError, ValueError):
        return None


# ─── Effective status (what the dashboard shows) ───────────


def _row(channel: str, stored: dict[str, Any] | None, **override: Any) -> dict[str, Any]:
    stored = stored or {}
    row = {
        "channel": channel,
        "label": LABELS[channel],
        "state": stored.get("state") or UNKNOWN,
        "account_name": stored.get("account_name"),
        "profile_url": stored.get("profile_url"),
        "last_checked_at": stored.get("last_checked_at"),
        "detail": stored.get("detail") if stored else "Not checked yet",
    }
    row.update(override)
    return row


def linkedin_row(values: Mapping[str, str]) -> dict[str, Any]:
    stored = _parse(values.get(STATUS_PREFIX + "linkedin"))
    if not settings.linkedin_enabled:
        return _row("linkedin", stored, state=DISABLED, detail="Switched off (LINKEDIN_ENABLED=false)")
    if is_truthy(values.get(LINKEDIN_DISABLED_KEY)):
        reason = values.get(LINKEDIN_REASON_KEY) or ""
        if reason == NEEDS_LOGIN_REASON:
            return _row("linkedin", stored, state=NEEDS_LOGIN)
        return _row("linkedin", stored, state=DISABLED, detail=f"Stopped: {reason.replace('_', ' ')}" if reason else "Stopped after errors")
    if stored and stored.get("state") == DISABLED:  # checked while switched off, switched on since
        return _row("linkedin", stored, state=UNKNOWN, detail="Not checked since it was switched on")
    return _row("linkedin", stored)


def outlook_row(values: Mapping[str, str]) -> dict[str, Any]:
    from app.channels.outlook import OutlookSettings

    stored = _parse(values.get(STATUS_PREFIX + "outlook"))
    cfg = OutlookSettings()
    if is_truthy(values.get(EMAIL_DISABLED_KEY)):
        return _row("outlook", stored, state=DISABLED, detail="Stopped after errors")
    if not cfg.ms_client_id:
        return _row("outlook", stored, state=ERROR, detail="MS_CLIENT_ID is not set")
    if not Path(cfg.ms_token_cache_path).exists():
        return _row("outlook", stored, state=NEEDS_LOGIN, detail="No sign-in yet: run the device-code login (README step 5)")
    return _row("outlook", stored)


def whatsapp_row(values: Mapping[str, str]) -> dict[str, Any]:
    stored = _parse(values.get(STATUS_PREFIX + "whatsapp"))
    if not settings.whatsapp_phone_number_id or not settings.whatsapp_token:
        return _row("whatsapp", stored, state=ERROR, detail="WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_TOKEN is not set")
    return _row("whatsapp", stored)


def linkedin_status(db: Session) -> dict[str, Any]:
    return linkedin_row(read_snapshot(db))


def outlook_status(db: Session) -> dict[str, Any]:
    return outlook_row(read_snapshot(db))


def whatsapp_status(db: Session) -> dict[str, Any]:
    return whatsapp_row(read_snapshot(db))


def all_statuses(db: Session) -> list[dict[str, Any]]:
    """Saved status only: three rows from one query, no browser and no network call."""
    values = read_snapshot(db)
    return [linkedin_row(values), outlook_row(values), whatsapp_row(values)]


def needs_login_from(values: Mapping[str, str]) -> bool:
    return linkedin_row(values)["state"] == NEEDS_LOGIN


def linkedin_needs_login(db: Session) -> bool:
    return needs_login_from(read_snapshot(db))


# ─── Applying a LinkedIn result ────────────────────────────


def _open_reconnect_items(db: Session) -> list[AttentionItem]:
    items = db.scalars(
        select(AttentionItem).where(
            AttentionItem.type == AttentionType.CHANNEL_ERROR, AttentionItem.status == AttentionStatus.OPEN
        )
    )
    return [
        item for item in items
        if (item.payload or {}).get("channel") == "linkedin" and (item.payload or {}).get("reason") == NEEDS_LOGIN_REASON
    ]


def apply_linkedin(db: Session, status: SessionStatus, now: datetime | None = None) -> bool:
    """Store the result and stop or resume LinkedIn. Returns True when a new reconnect alert is due."""
    now = now or utc_now()
    save_status(db, "linkedin", status, now)
    alert = False
    disabled = is_truthy(_get(db, LINKEDIN_DISABLED_KEY))
    reason = _get(db, LINKEDIN_REASON_KEY) or ""
    if status.state == NEEDS_LOGIN:
        if not disabled:
            _put(db, LINKEDIN_DISABLED_KEY, "true")
            _put(db, LINKEDIN_REASON_KEY, NEEDS_LOGIN_REASON)
        if not _open_reconnect_items(db):
            db.add(AttentionItem(
                type=AttentionType.CHANNEL_ERROR,
                payload={
                    "channel": "linkedin",
                    "reason": NEEDS_LOGIN_REASON,
                    "title": RECONNECT_TITLE,
                    "detail": status.detail,
                    "action": f"Connect LinkedIn in Settings > Channels, or run `{linkedin_session.LOGIN_COMMAND}`.",
                },
            ))
            alert = True
    elif status.state == CONNECTED:
        if disabled and reason == NEEDS_LOGIN_REASON:
            _put(db, LINKEDIN_DISABLED_KEY, "false")
            _put(db, LINKEDIN_REASON_KEY, "")
        for item in _open_reconnect_items(db):
            item.status = AttentionStatus.RESOLVED
    db.commit()
    return alert


async def alert_reconnect(status: SessionStatus, notify: Notify | None = None) -> None:
    text = (
        f"{RECONNECT_TITLE}: the session needs a login ({status.detail or 'signed out'}). All LinkedIn "
        f"actions are stopped. Use Connect LinkedIn in the dashboard or run `{linkedin_session.LOGIN_COMMAND}`."
    )
    try:
        if notify is None:
            from app.workers.review import whatsapp_notify as notify
        await notify(text)
    except Exception as exc:
        logger.error(f"WhatsApp alert for LinkedIn login failed: {type(exc).__name__}")


async def record_linkedin(
    status: SessionStatus,
    session_factory: Callable[[], Session] | None = None,
    notify: Notify | None = None,
) -> SessionStatus:
    """Persist a LinkedIn session result (from a check or a login) and alert the owner if it needs a login."""
    if session_factory is None:
        from app.db import SessionLocal as session_factory
    db = session_factory()
    try:
        alert = apply_linkedin(db, status)
    finally:
        db.close()
    if alert:
        await alert_reconnect(status, notify)
    return status


async def check_linkedin(
    checker: Checker | None = None,
    session_factory: Callable[[], Session] | None = None,
    notify: Notify | None = None,
) -> SessionStatus:
    status = await (checker or linkedin_session.check_session)()
    return await record_linkedin(status, session_factory, notify)


async def linkedin_preflight(
    checker: Checker | None = None,
    session_factory: Callable[[], Session] | None = None,
    notify: Notify | None = None,
    now: datetime | None = None,
) -> str:
    """Run before each LinkedIn work batch. Re-checks the session unless a recent check was fine.

    Returns the state the batch should assume; never raises.
    """
    if session_factory is None:
        from app.db import SessionLocal as session_factory
    now = now or utc_now()
    db = session_factory()
    try:
        current = linkedin_status(db)
    finally:
        db.close()
    if current["state"] in (DISABLED, NEEDS_LOGIN):
        return current["state"]  # LinkedIn is already stopped; the batch skips it
    checked = _last_checked(current)
    # A recent result stands, also a failed check, so a broken browser is not relaunched every minute.
    if current["state"] in (CONNECTED, ERROR) and checked is not None and now - checked < PREFLIGHT_MAX_AGE:
        return current["state"]
    try:
        return (await check_linkedin(checker, session_factory, notify)).state
    except Exception as exc:
        logger.error(f"LinkedIn preflight failed: {type(exc).__name__}: {exc}")
        return ERROR


# ─── Outlook and WhatsApp checks ───────────────────────────


def _outlook_silent() -> SessionStatus:
    """Silent token refresh from the cache. Never starts a device-code flow (that needs a person)."""
    from app.channels.outlook import SCOPES, OutlookSettings, TokenProvider

    cfg = OutlookSettings()
    if not cfg.ms_client_id:
        return SessionStatus(ERROR, detail="MS_CLIENT_ID is not set")
    if not Path(cfg.ms_token_cache_path).exists():
        return SessionStatus(NEEDS_LOGIN, detail="No sign-in yet: run the device-code login (README step 5)")
    tokens = TokenProvider(cfg.ms_client_id, cfg.ms_tenant, cfg.ms_token_cache_path)
    accounts = tokens.app.get_accounts()
    if not accounts:
        return SessionStatus(NEEDS_LOGIN, detail="The token cache holds no account")
    result = tokens.app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        return SessionStatus(NEEDS_LOGIN, detail="The saved sign-in expired: run the device-code login again")
    tokens._save_cache()
    return SessionStatus(CONNECTED, account_name=accounts[0].get("username"))


async def check_outlook() -> SessionStatus:
    try:
        return await asyncio.to_thread(_outlook_silent)
    except Exception as exc:
        return SessionStatus(ERROR, detail=f"{type(exc).__name__}: {exc}"[:300])


async def check_whatsapp(http: httpx.AsyncClient | None = None) -> SessionStatus:
    """Reads the business phone number's name with the access token (no message is sent)."""
    from app.channels.whatsapp import GRAPH_URL, TIMEOUT_SECONDS

    if not settings.whatsapp_phone_number_id or not settings.whatsapp_token:
        return SessionStatus(ERROR, detail="WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_TOKEN is not set")
    url = f"{GRAPH_URL}/{settings.whatsapp_phone_number_id}"
    params = {"fields": "display_phone_number,verified_name"}
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}
    try:
        if http is not None:
            response = await http.get(url, params=params, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        return SessionStatus(ERROR, detail=f"{type(exc).__name__}: {exc}"[:300])
    try:
        data = response.json()
    except ValueError:
        data = {}
    error_code = (data.get("error") or {}).get("code") if isinstance(data, dict) else None
    if response.status_code == 401 or error_code == TOKEN_EXPIRED_CODE:
        return SessionStatus(NEEDS_LOGIN, detail="The WhatsApp access token is invalid or expired")
    if response.status_code >= 400:
        return SessionStatus(ERROR, detail=f"WhatsApp API {response.status_code}")
    name = data.get("verified_name")
    number = data.get("display_phone_number")
    account = f"{name} ({number})" if name and number else name or number
    return SessionStatus(CONNECTED, account_name=account)


async def check_other(channel: str, db: Session) -> None:
    """Run the Outlook or WhatsApp check now and store the result."""
    status = await (check_outlook() if channel == "outlook" else check_whatsapp())
    save_status(db, channel, status, utc_now())
    db.commit()
