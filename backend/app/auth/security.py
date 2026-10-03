"""Password hashing, session tokens, session cookie and the CSRF header.

The session is a signed JWT in an httpOnly cookie. It carries only the user id and
a fingerprint of the password hash; the role is always read from the database, so
disabling a user or changing a role takes effect on the next request.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, Response, status

from app.config import settings
from app.db.models import User

ALGORITHM = "HS256"
MIN_SECRET_LENGTH = 16
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128  # bounds the work a single request can ask argon2 to do

SESSION_COOKIE = "connectx_session"
CSRF_HEADER = "X-CSRF-Protection"
CSRF_VALUE = "1"

_hasher = PasswordHasher()  # argon2id


def auth_configured() -> bool:
    return len(settings.jwt_secret) >= MIN_SECRET_LENGTH


def require_configured() -> None:
    if not auth_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Dashboard login is not configured. Set JWT_SECRET (16+ characters).",
        )


# ─── Passwords ─────────────────────────────────────────────


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash("not-a-real-password")


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Always runs a full argon2 verification, even without a stored hash.

    An unknown account therefore costs the same time as a wrong password, so response
    time does not reveal which emails are registered.
    """
    try:
        matched = _hasher.verify(stored_hash or _dummy_hash(), password)
    except (VerificationError, InvalidHashError):
        return False
    return matched and stored_hash is not None


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


# ─── Session token and cookie ──────────────────────────────


def password_fingerprint(user: User) -> str:
    """Changes whenever the password changes, which ends every session issued before it."""
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def create_session_token(user: User, now: datetime | None = None) -> tuple[str, int]:
    now = now or datetime.now(timezone.utc)
    lifetime = timedelta(hours=settings.jwt_expire_hours)
    payload = {
        "sub": str(user.id),
        "pv": password_fingerprint(user),
        "iat": now,
        "exp": now + lifetime,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM), int(lifetime.total_seconds())


def decode_session_token(token: str) -> dict:
    return jwt.decode(
        token, settings.jwt_secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub", "pv"]}
    )


def cookie_secure() -> bool:
    return settings.app_env == "production"


def set_session_cookie(response: Response, user: User) -> None:
    token, max_age = create_session_token(user)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE, httponly=True, samesite="lax", secure=cookie_secure(), path="/"
    )


# ─── CSRF ──────────────────────────────────────────────────


def require_csrf_header(request: Request) -> None:
    """State-changing requests must carry a custom header.

    Browsers only send a custom header cross-origin after a CORS preflight, and this API
    allows no cross-origin requests, so a forged form or fetch from another site fails.
    """
    supplied = request.headers.get(CSRF_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), CSRF_VALUE.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing {CSRF_HEADER} header")
