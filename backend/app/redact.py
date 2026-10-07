"""Keep secrets out of logs and error messages.

redact() masks API keys, Bearer tokens, "key=..." / "token=..." values and the exact secrets held in
settings. install() wraps the log record factory, so every record from every logger (httpx, httpcore,
uvicorn, our own) is redacted before any handler sees it, whatever handlers are configured later.
"""

from __future__ import annotations

import logging
import re

from app.config import settings

MASK = "[REDACTED]"
MIN_SECRET_LENGTH = 8

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {MASK}"),
    (re.compile(r"(?i)(x-goog-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',&]+"), rf"\1{MASK}"),
    (re.compile(r"(?i)([a-z0-9_-]*(?:key|token|secret)=)[^&\s\"'#]+"), rf"\1{MASK}"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), MASK),
    (re.compile(r"\bgsk_[0-9A-Za-z]{20,}"), MASK),
    (re.compile(r"\bsk-[0-9A-Za-z_-]{20,}"), MASK),
]
_SECRET_SETTINGS = (
    "groq_api_key",
    "gemini_api_key",
    "ollama_api_key",
    "whatsapp_token",
    "whatsapp_app_secret",
    "whatsapp_verify_token",
    "jwt_secret",
    "ms_auth_client_secret",
    "postgres_password",
)


def redact(text: object) -> str:
    result = str(text)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    for name in _SECRET_SETTINGS:
        secret = getattr(settings, name, "")
        if isinstance(secret, str) and len(secret) >= MIN_SECRET_LENGTH:
            result = result.replace(secret, MASK)
    return result


def _redact_args(args: object) -> object:
    if isinstance(args, tuple):
        return tuple(redact(a) if isinstance(a, str) else a for a in args)
    return args


def install() -> None:
    """Idempotent: redact every log record (message, arguments and exception text) at creation."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "redacting", False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            if record.name == "uvicorn.access":  # its formatter unpacks the args, so keep their shape
                record.args = _redact_args(record.args)
            else:
                record.msg = redact(record.getMessage())
                record.args = None
            if record.exc_info and record.exc_info[0] is not None:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        except Exception:  # logging must never break the caller
            pass
        return record

    factory.redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
