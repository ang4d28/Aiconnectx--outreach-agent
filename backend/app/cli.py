"""Operator commands. Run inside the backend container or virtualenv:

    python -m app.cli create-admin --email owner@example.com

This is the only way to create an admin. OAuth sign-ins and registrations always start as
`pending`, and no email claim from Google or Microsoft ever grants a role.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Callable, Sequence

from sqlalchemy import select

from app.auth.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password
from app.api.inquiries import EMAIL_PATTERN
from app.db.models import AuthProvider, User, UserRole

Prompt = Callable[[str], str]


class CliError(Exception):
    pass


def create_admin(email: str, name: str | None, prompt: Prompt = getpass.getpass) -> str:
    """Create an email+password admin, or make an existing password account an admin.

    For an existing account the password prompt sets a new password, so this also recovers
    access when the last admin is locked out.
    """
    from app.db import SessionLocal

    email = email.strip().lower()
    if not EMAIL_PATTERN.match(email) or len(email) > 254:
        raise CliError("That is not a valid email address.")
    password = prompt("Password (min 10 characters): ")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise CliError(f"The password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters.")
    if prompt("Repeat password: ") != password:
        raise CliError("The passwords do not match.")

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.provider == AuthProvider.PASSWORD, User.email == email))
        if user is None:
            db.add(
                User(
                    email=email,
                    name=(name or email.split("@")[0]).strip()[:120],
                    password_hash=hash_password(password),
                    provider=AuthProvider.PASSWORD,
                    role=UserRole.ADMIN,
                )
            )
            result = f"Created admin {email}."
        else:
            user.password_hash = hash_password(password)
            user.role = UserRole.ADMIN
            result = f"{email} already existed: it is now an admin with the new password."
        db.commit()
    return result


def main(argv: Sequence[str] | None = None, prompt: Prompt = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="create an admin account (prompts for a password)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", help="display name (default: the part of the email before @)")
    args = parser.parse_args(argv)

    try:
        print(create_admin(args.email, args.name, prompt))
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
