"""Dashboard accounts: email + password and Microsoft sign-in.

Every new account (registration or first OAuth sign-in) is `pending` and can do nothing
but call /me until an admin approves it. Accounts are never merged across providers:
the same email on password, Google and Microsoft makes three separate accounts.
The first admin is created from the command line (python -m app.cli create-admin).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.inquiries import CONTROL_CHARS, EMAIL_PATTERN, RateLimiter, client_id
from app.auth import oauth
from app.auth.deps import get_current_user
from app.auth.security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    clear_session_cookie,
    hash_password,
    needs_rehash,
    require_configured,
    require_csrf_header,
    set_session_cookie,
    verify_password,
)
from app.config import settings
from app.db import get_session
from app.db.models import AuthProvider, User, UserRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

ATTEMPTS_PER_MINUTE = 5
INVALID_LOGIN = "Invalid email or password"
NOT_ADMIN = "This account is not an administrator"
REGISTERED ="Registration received. An administrator has to approve your account before you can sign in."


class AuthLimiters:
    """5 per minute per client IP and 5 per minute per email, for login and for registration."""

    def __init__(self) -> None:
        self.login_ip = RateLimiter(limit=ATTEMPTS_PER_MINUTE, window=60)
        self.login_email = RateLimiter(limit=ATTEMPTS_PER_MINUTE, window=60)
        self.register_ip = RateLimiter(limit=ATTEMPTS_PER_MINUTE, window=60)
        self.register_email = RateLimiter(limit=ATTEMPTS_PER_MINUTE, window=60)


limiters = AuthLimiters()


def enforce_rate_limit(by_ip: RateLimiter, by_email: RateLimiter, request: Request, email: str) -> None:
    allowed_ip = by_ip.allow(client_id(request))
    allowed_email = by_email.allow(email)
    if not (allowed_ip and allowed_email):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Try again in a minute.",
            headers={"Retry-After": "60"},
        )


def normalize_email(value: str) -> str:
    return CONTROL_CHARS.sub("", value).strip().lower()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserRead(BaseModel):
    id: int
    email: str
    name: str
    provider: AuthProvider
    role: UserRole
    created_at: datetime | None = None
    last_login_at: datetime | None = None


def to_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        email=user.email,
        name=user.name,
        provider=user.provider,
        role=user.role,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=254)
    name: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, value: object) -> object:
        return normalize_email(value) if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not EMAIL_PATTERN.match(value):
            raise ValueError("invalid email address")
        return value

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, value: object) -> object:
        return CONTROL_CHARS.sub("", value).strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be blank")
        return value


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=1, max_length=MAX_PASSWORD_LENGTH)
    # Set by the Admin tab. The tab grants nothing: the server checks the role.
    admin_only: bool = False


def find_password_user(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.provider == AuthProvider.PASSWORD, User.email == email))


# ─── Email + password ──────────────────────────────────────


@router.post("/register", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_csrf_header)])
def register(data: RegisterRequest, request: Request, db: Session = Depends(get_session)) -> dict[str, str]:
    require_configured()
    enforce_rate_limit(limiters.register_ip, limiters.register_email, request, data.email)
    password_hash = hash_password(data.password)  # also when the email is taken: same work, same answer
    if find_password_user(db, data.email) is None:
        db.add(
            User(
                email=data.email,
                name=data.name,
                password_hash=password_hash,
                provider=AuthProvider.PASSWORD,
                role=UserRole.PENDING,
            )
        )
        try:
            db.commit()
        except IntegrityError:  # a parallel request registered the same email first
            db.rollback()
    return {"status": "pending_approval", "message": REGISTERED}


@router.post("/login", dependencies=[Depends(require_csrf_header)])
def login(
    data: LoginRequest, request: Request, response: Response, db: Session = Depends(get_session)
) -> dict[str, Any]:
    require_configured()
    email = normalize_email(data.email)
    enforce_rate_limit(limiters.login_ip, limiters.login_email, request, email)
    user = find_password_user(db, email)
    password_ok = verify_password(user.password_hash if user else None, data.password)
    # One message for an unknown email, a wrong password and a disabled account.
    if user is None or not password_ok or user.role == UserRole.DISABLED:
        logger.warning(f"Failed dashboard login from {client_id(request)}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_LOGIN)
    # Only after the password checked out, so this cannot be used to find out who is an admin.
    if data.admin_only and user.role != UserRole.ADMIN:
        logger.warning(f"Admin login refused for a non-admin account from {client_id(request)}")
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_ADMIN)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password)
    user.last_login_at = utc_now()
    db.commit()
    set_session_cookie(response, user)
    return {"user": to_read(user)}


@router.post("/logout", dependencies=[Depends(require_csrf_header)])
def logout(response: Response) -> dict[str, str]:
    """Clears the cookie. Sessions end by expiry, by disabling the user or by a password reset."""
    clear_session_cookie(response)
    return {"status": "signed_out"}


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> UserRead:
    """Works for pending and disabled accounts too, so the UI can tell them why they are locked out."""
    return to_read(user)


# ─── Microsoft ──────────────────────────────────


def frontend_redirect(path: str = "/dashboard", error: str | None = None) -> RedirectResponse:
    url = settings.frontend_url.rstrip("/") + path
    if error:
        url += f"?auth_error={error}"
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


async def oauth_login(provider: AuthProvider, request: Request) -> Response:
    require_configured()
    if not oauth.provider_configured(provider):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{provider.value.title()} sign-in is not configured")
    if "session" not in request.scope:
        logger.error("OAuth sign-in needs the session middleware (app.auth.oauth.install_session_middleware)")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Sign-in is not available")
    return await oauth.get_client(provider).authorize_redirect(request, oauth.redirect_uri(provider))


async def verified_identity(provider: AuthProvider, request: Request) -> oauth.OAuthIdentity | str:
    """The identity from a verified ID token, or the error code for the redirect."""
    try:
        token = await oauth.get_client(provider).authorize_access_token(
            request, claims_options=oauth.claims_options(provider)
        )
    except Exception as exc:  # state, PKCE, token exchange or ID-token failure: all fail closed
        logger.warning(f"{provider.value} sign-in rejected: {type(exc).__name__}: {exc}")
        return "denied" if request.query_params.get("error") else "oauth_failed"
    claims = token.get("userinfo")
    identity = oauth.identity_from_claims(provider, dict(claims)) if claims else None
    return identity or "unusable_profile"


async def oauth_callback(provider: AuthProvider, request: Request, db: Session) -> Response:
    require_configured()
    if not oauth.provider_configured(provider) or "session" not in request.scope:
        return frontend_redirect(error="not_configured")
    identity = await verified_identity(provider, request)
    request.session.clear()
    if isinstance(identity, str):
        return frontend_redirect(error=identity)

    user = db.scalar(
        select(User).where(User.provider == provider, User.provider_subject == identity.subject)
    )
    if user is None:
        same_email = db.scalar(select(User).where(User.provider == provider, User.email == identity.email))
        if same_email is not None:
            # Same provider, same email, different subject. Never take that account over.
            logger.warning(f"{provider.value} sign-in refused: email already belongs to another {provider.value} account")
            return frontend_redirect(error="account_conflict")
        user = User(
            email=identity.email,
            name=identity.name,
            provider=provider,
            provider_subject=identity.subject,
            role=UserRole.PENDING,
        )
        db.add(user)
    if user.role == UserRole.DISABLED:
        return frontend_redirect(error="disabled")
    user.last_login_at = utc_now()
    try:
        db.commit()
    except IntegrityError:  # a parallel callback created the same account
        db.rollback()
        return frontend_redirect(error="account_conflict")
    response = frontend_redirect()
    set_session_cookie(response, user)
    return response


@router.get("/microsoft/login")
async def microsoft_login(request: Request) -> Response:
    return await oauth_login(AuthProvider.MICROSOFT, request)


@router.get("/microsoft/callback")
async def microsoft_callback(request: Request, db: Session = Depends(get_session)) -> Response:
    return await oauth_callback(AuthProvider.MICROSOFT, request, db)
