"""User management (admin only: /api/admin is admin-only in app.auth.deps).

Admins approve pending accounts, change roles, disable accounts and reset passwords.
They cannot change their own role or disable themselves, so the system always keeps
the admin who is acting. The first admin comes from `python -m app.cli create-admin`.
"""

from __future__ import annotations

import logging
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import UserRead, to_read
from app.auth.deps import get_current_user
from app.auth.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password
from app.db import get_session
from app.db.models import AuthProvider, User, UserRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["admin"])

GrantableRole = Literal["admin", "member", "viewer"]


class ApproveRequest(BaseModel):
    role: GrantableRole = "viewer"


class RoleRequest(BaseModel):
    role: GrantableRole


class ResetPasswordRequest(BaseModel):
    password: str | None = Field(None, min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


def get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


def not_self(admin: User, target: User, action: str) -> None:
    if admin.id == target.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"You cannot {action} your own account")


@router.get("")
def list_users(
    role: UserRole | None = None,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    stmt = select(User)
    if role is not None:
        stmt = stmt.where(User.role == role)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    users = db.scalars(stmt.order_by(User.id).limit(limit))
    return {"items": [to_read(user) for user in users], "total": total}


@router.post("/{user_id}/approve", response_model=UserRead)
def approve(
    user_id: int,
    data: ApproveRequest | None = None,
    admin: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    user = get_user_or_404(db, user_id)
    if user.role != UserRole.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only pending accounts can be approved")
    user.role = UserRole((data or ApproveRequest()).role)
    db.commit()
    logger.warning(f"Admin {admin.id} approved user {user.id} as {user.role.value}")
    return to_read(user)


@router.patch("/{user_id}/role", response_model=UserRead)
def change_role(
    user_id: int,
    data: RoleRequest,
    admin: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    user = get_user_or_404(db, user_id)
    not_self(admin, user, "change the role of")
    user.role = UserRole(data.role)
    db.commit()
    logger.warning(f"Admin {admin.id} set user {user.id} to {user.role.value}")
    return to_read(user)


@router.post("/{user_id}/disable", response_model=UserRead)
def disable(
    user_id: int,
    admin: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    user = get_user_or_404(db, user_id)
    not_self(admin, user, "disable")
    user.role = UserRole.DISABLED
    db.commit()
    logger.warning(f"Admin {admin.id} disabled user {user.id}")
    return to_read(user)


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    data: ResetPasswordRequest | None = None,
    admin: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    """Sets a new password (the one given, or a generated one returned once) and ends the user's sessions."""
    user = get_user_or_404(db, user_id)
    if user.provider != AuthProvider.PASSWORD:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"This account signs in with {user.provider.value}; it has no password"
        )
    new_password = data.password if data and data.password else None
    generated = new_password is None
    if generated:
        new_password = secrets.token_urlsafe(16)
    user.password_hash = hash_password(new_password)
    db.commit()
    logger.warning(f"Admin {admin.id} reset the password of user {user.id}")
    return {"id": user.id, "temporary_password": new_password if generated else None}
