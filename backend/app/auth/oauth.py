"""Microsoft sign-in with OpenID Connect (authlib).

authlib creates the state, nonce and PKCE (S256) values, keeps them in the short-lived
OAuth session cookie, and on the callback checks the state, exchanges the code with the
code verifier and validates the ID token (signature from the provider's JWKS, issuer,
audience, expiry, nonce). Nothing here ever grants a role: new accounts are `pending`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.db.models import AuthProvider

OAUTH_SESSION_COOKIE = "connectx_oauth"
OAUTH_SESSION_SECONDS = 600

MICROSOFT_METADATA_URL = "https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
MICROSOFT_MULTI_TENANT = frozenset({"common", "organizations", "consumers"})
# Multi-tenant discovery documents list a `{tenantid}` placeholder as the issuer, so the
# real issuer (a tenant GUID) is checked against this pattern instead.
MICROSOFT_ISSUER = re.compile(r"https://login\.microsoftonline\.com/[0-9a-fA-F-]{36}/v2\.0")

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

PROVIDERS = (AuthProvider.MICROSOFT,)

_registry: OAuth | None = None


def install_session_middleware(app: FastAPI) -> None:
    """The OAuth flow keeps its state in a signed cookie, so the app needs this middleware."""
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.jwt_secret,
        session_cookie=OAUTH_SESSION_COOKIE,
        max_age=OAUTH_SESSION_SECONDS,
        same_site="lax",  # lax so the cookie comes back on the redirect from the provider
        https_only=settings.app_env == "production",
    )


def credentials(provider: AuthProvider) -> tuple[str, str]:
    return settings.ms_auth_client_id, settings.ms_auth_client_secret


def provider_configured(provider: AuthProvider) -> bool:
    return all(credentials(provider))


def redirect_uri(provider: AuthProvider) -> str:
    return f"{settings.backend_url.rstrip('/')}/api/auth/{provider.value}/callback"


def reset_registry() -> None:
    global _registry
    _registry = None


def get_client(provider: AuthProvider) -> Any:
    global _registry
    if _registry is None:
        _registry = OAuth()
        for name in PROVIDERS:
            client_id, client_secret = credentials(name)
            metadata_url = MICROSOFT_METADATA_URL.format(tenant=settings.ms_auth_tenant)
            _registry.register(
                name=name.value,
                client_id=client_id,
                client_secret=client_secret,
                server_metadata_url=metadata_url,
                client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
            )
    return getattr(_registry, provider.value)


def claims_options(provider: AuthProvider) -> dict[str, Any] | None:
    """Issuer check override for Microsoft's multi-tenant endpoints; None keeps the default."""
    if provider == AuthProvider.MICROSOFT and settings.ms_auth_tenant.lower() in MICROSOFT_MULTI_TENANT:
        return {
            "iss": {
                "essential": True,
                "validate": lambda _claims, issuer: bool(MICROSOFT_ISSUER.fullmatch(str(issuer))),
            }
        }
    return None


@dataclass(frozen=True)
class OAuthIdentity:
    provider: AuthProvider
    subject: str
    email: str
    name: str


def identity_from_claims(provider: AuthProvider, claims: dict[str, Any]) -> OAuthIdentity | None:
    """The verified ID-token claims as an identity, or None if they are unusable.

    Only `sub` identifies the person. The email is stored for display and for the
    per-provider uniqueness rule; it never links this account to another provider's.
    """
    subject = str(claims.get("sub") or "").strip()
    email = str(claims.get("email") or claims.get("preferred_username") or "")
    email = email.strip().lower()
    if not subject or len(subject) > 255 or not EMAIL_PATTERN.match(email) or len(email) > 254:
        return None
    name = str(claims.get("name") or "").strip()[:120] or email.split("@")[0]
    return OAuthIdentity(provider=provider, subject=subject, email=email, name=name)
