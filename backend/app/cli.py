"""Operator commands. Run inside the backend container or virtualenv:

    python -m app.cli create-admin --email owner@example.com
    python -m app.cli test-channel outlook --to you@example.com

This is the only way to create an admin. OAuth sign-ins and registrations always start as
`pending`, and no email claim from Microsoft ever grants a role.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from typing import Callable, Sequence

from sqlalchemy import select

from app.auth.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password
from app.api.inquiries import EMAIL_PATTERN
from app.channels.base import Thread
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


TEST_SUBJECT = "AI ConnectX channel test"
TEST_TEXT = "This is a test message from AI ConnectX. No reply is needed."
RECENT_REPLY_DAYS = 7
CHANNELS = ("outlook", "whatsapp")


def _show(reply: object) -> str:
    sender = getattr(reply, "sender", "?")
    when = getattr(reply, "received_at", "")
    text = " ".join((getattr(reply, "text", "") or "").split())[:80]
    return f"  {when}  {sender}: {text}"


async def run_channel_test(channel: str, to: str) -> int:
    """Send one short test message, then list recent replies the channel can read. Ignores DRY_RUN (you asked
    for a real test) but respects SEND_ALLOWLIST. Returns the exit code."""
    from datetime import datetime, timedelta, timezone

    from app import sandbox

    print(sandbox.startup_line())
    try:
        if channel == "outlook":
            from app.channels import outlook

            sandbox.check_email(to)
            client = outlook.get_client()
            await client.send_message(Thread(recipient=to, subject=TEST_SUBJECT), TEST_TEXT)
        else:
            from app.channels.whatsapp import whatsapp_client

            sandbox.check_whatsapp(to)
            await whatsapp_client.send_text(TEST_TEXT, to=to)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"OK: test message sent via {channel} to {to}.")

    if channel == "whatsapp":
        print("Replies: WhatsApp has no read API; inbound messages only arrive through the webhook.")
        return 0
    try:
        since = datetime.now(timezone.utc) - timedelta(days=RECENT_REPLY_DAYS)
        replies = await client.read_new_replies(since)
    except Exception as exc:
        print(f"Replies: could not read ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    print(f"Replies in the last {RECENT_REPLY_DAYS} days: {len(replies)}")
    for reply in replies[-10:]:
        print(_show(reply))
    return 0


def main(argv: Sequence[str] | None = None, prompt: Prompt = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="create an admin account (prompts for a password)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", help="display name (default: the part of the email before @)")
    test = commands.add_parser("test-channel", help="send one test message and list recent replies")
    test.add_argument("channel", choices=CHANNELS)
    test.add_argument("--to", required=True, help="email address (outlook) or phone number (whatsapp)")
    args = parser.parse_args(argv)

    if args.command == "test-channel":
        return asyncio.run(run_channel_test(args.channel, args.to))

    try:
        print(create_admin(args.email, args.name, prompt))
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
