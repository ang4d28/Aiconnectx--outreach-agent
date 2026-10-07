"""Who is calling, and what they may do.

get_current_user  any valid session (also pending and disabled accounts; /me uses it).
authorize         the dependency on every protected router: viewer to read, member for
                  the draft and attention actions and the Google Sheet sync listed in MEMBER_ACTIONS, admin for
                  everything else that changes data and for all of /api/admin and /api/channels.
                  A new mutating route is admin-only until it is added to MEMBER_ACTIONS.
"""

from __future__ import annotations

import hmac

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.security import (
    SESSION_COOKIE,
    decode_session_token,
    password_fingerprint,
    require_configured,
    require_csrf_header,
)
from app.db import get_session
from app.db.models import User, UserRole

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

RANK: dict[UserRole, int] = {
    UserRole.PENDING: 0,
    UserRole.DISABLED: 0,
    UserRole.VIEWER: 1,
    UserRole.MEMBER: 2,
    UserRole.ADMIN: 3,
}

ADMIN_ONLY_PREFIXES = ("/api/admin", "/api/channels")

# (method, route path template) of the writes a member may do: edit, approve and cancel
# drafts, and resolve attention items.
MEMBER_ACTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/drafts/{draft_id}/approve"),
        ("POST", "/api/drafts/{draft_id}/reject"),
        ("PATCH", "/api/drafts/{draft_id}"),
        ("POST", "/api/drafts/{draft_id}/regenerate"),
        ("POST", "/api/drafts/{draft_id}/restore"),
        ("POST", "/api/drafts/regenerate-all"),
        ("POST", "/api/dashboard/drafts/{draft_id}/approve"),
        ("POST", "/api/dashboard/drafts/{draft_id}/cancel"),
        ("PATCH", "/api/dashboard/drafts/{draft_id}"),
        ("POST", "/api/dashboard/drafts/bulk-approve"),
        ("POST", "/api/attention/{item_id}/approve"),
        ("POST", "/api/attention/{item_id}/dismiss"),
        ("PATCH", "/api/attention/{item_id}"),
        ("POST", "/api/sheet/sync"),
        ("PATCH", "/api/inquiries/{inquiry_id}"),
    }
)


def unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail)


def get_current_user(request: Request, db: Session = Depends(get_session)) -> User:
    require_configured()
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise unauthorized()
    try:
        claims = decode_session_token(token)
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, ValueError):
        raise unauthorized("Session expired or invalid")
    user = db.get(User, user_id)
    if user is None or not hmac.compare_digest(claims["pv"], password_fingerprint(user)):
        raise unauthorized("Session expired or invalid")
    return user


def route_template(request: Request) -> str:
    """The matched route's full path template, e.g. /api/attention/{item_id}/dismiss.

    Newer FastAPI versions report the route path without the prefix of the router it was included
    through, so the leading part is taken from the request path and the route's own segments after it.
    """
    path = request.url.path
    route_path = getattr(request.scope.get("route"), "path", None)
    if not route_path:
        return path
    request_segments = path.strip("/").split("/")
    route_segments = route_path.strip("/").split("/")
    if len(request_segments) < len(route_segments) or ":path}" in route_path:
        return route_path
    prefix = request_segments[: len(request_segments) - len(route_segments)]
    return "/" + "/".join(prefix + route_segments)


def required_role(request: Request) -> UserRole:
    route_path = route_template(request)
    if route_path.startswith(ADMIN_ONLY_PREFIXES):
        return UserRole.ADMIN
    if request.method in SAFE_METHODS:
        return UserRole.VIEWER
    if (request.method, route_path) in MEMBER_ACTIONS:
        return UserRole.MEMBER
    return UserRole.ADMIN


def authorize(request: Request, user: User = Depends(get_current_user)) -> User:
    if user.role == UserRole.PENDING:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account is waiting for administrator approval")
    if user.role == UserRole.DISABLED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account is disabled")
    if RANK[user.role] < RANK[required_role(request)]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have permission to do this")
    if request.method not in SAFE_METHODS:
        require_csrf_header(request)
    return user
