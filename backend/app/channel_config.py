"""Which settings each channel needs, and which scheduler jobs only exist for that channel.

A channel with a missing setting is SKIPPED (never an error): the scheduler does not register its own
jobs and `python -m app.cli doctor` names the missing settings. Jobs shared with other channels
(send_queue, replies ...) stay registered; the workers skip an unavailable channel themselves.
"""

from __future__ import annotations

from app.config import settings

REQUIRED_SETTINGS: dict[str, tuple[str, ...]] = {
    "outlook": ("MS_CLIENT_ID",),
    "whatsapp": (
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_TOKEN",
        "WHATSAPP_VERIFY_TOKEN",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_OWNER_NUMBER",
    ),
    "linkedin": ("LINKEDIN_PROFILE_DIR",),
}
CHANNEL_JOBS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin_session", "connection_poll"),
    "whatsapp": ("daily_summary",),  # the summary only goes to WhatsApp
}


def missing_settings(channel: str) -> list[str]:
    return [
        name
        for name in REQUIRED_SETTINGS[channel]
        if not str(getattr(settings, name.lower(), "") or "").strip()
    ]


def unregistered_jobs() -> dict[str, str]:
    """Job id -> channel whose missing settings keep it from being registered."""
    return {job: channel for channel, jobs in CHANNEL_JOBS.items() if missing_settings(channel) for job in jobs}
