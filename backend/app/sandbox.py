"""Send allowlist ("sandbox"): when SEND_ALLOWLIST is set, email goes only to the addresses on it."""

from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


class SandboxBlocked(Exception):
    """The recipient is not on SEND_ALLOWLIST. Nothing was sent."""


def allowlist() -> set[str]:
    raw = (settings.send_allowlist or "").replace("\n", ",")
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def active() -> bool:
    return bool(allowlist())


def email_allowed(address: str) -> bool:
    entries = allowlist()
    return not entries or address.strip().lower() in entries


def check_email(address: str) -> None:
    if not email_allowed(address):
        logger.warning(f"blocked by sandbox: email to {address} is not on SEND_ALLOWLIST")
        raise SandboxBlocked(f"blocked by sandbox: {address} is not on SEND_ALLOWLIST")


def _digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


def check_whatsapp(number: str) -> None:
    """For the test command only (the app messages the owner number alone). With the allowlist on, the target
    must be the owner number or an allowlist entry written as a phone number."""
    entries = allowlist()
    if not entries:
        return
    wanted = _digits(number)
    allowed = {_digits(e) for e in entries if "@" not in e} | {_digits(settings.whatsapp_owner_number)}
    if not wanted or wanted not in allowed:
        logger.warning(f"blocked by sandbox: WhatsApp to {number} is not allowed")
        raise SandboxBlocked(f"blocked by sandbox: {number} is not the owner number or on SEND_ALLOWLIST")


def startup_line() -> str:
    mode = "DRY_RUN=true (nothing is sent)" if settings.dry_run else "DRY_RUN=false (LIVE)"
    entries = allowlist()
    sandbox = f"send allowlist ACTIVE ({len(entries)} address(es))" if entries else "send allowlist inactive"
    return f"Startup: {mode}; {sandbox}"
