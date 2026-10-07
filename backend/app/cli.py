"""Operator commands. Run inside the backend container or virtualenv:

    python -m app.cli create-admin --email owner@example.com
    python -m app.cli test-channel outlook --to you@example.com
    python -m app.cli linkedin-login
    python -m app.cli test-llm
    python -m app.cli doctor
    python -m app.cli run-job <post_monitor|enrich|draft|send_queue|release_held|replies|followups|sheets>

This is the only way to create an admin. OAuth sign-ins and registrations always start as
`pending`, and no email claim from Microsoft ever grants a role.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from typing import Any, Awaitable, Callable, Sequence

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


async def run_linkedin_login(login: Callable[[], Awaitable[Any]] | None = None) -> int:
    """Visible browser on the persistent profile at linkedin.com/login (LINKEDIN_HEADLESS is ignored).

    Waits up to 5 minutes for the feed, closes the browser and stores only the status (state, account
    name, profile URL, time). The password is typed into LinkedIn's own page and never seen here.
    """
    from app.channels import linkedin_session
    from app.channels.status import record_linkedin

    print("A browser window opens at linkedin.com/login. Sign in there (and solve any check by hand).")
    print("Waiting up to 5 minutes for the LinkedIn feed...")
    result = await (login or linkedin_session.interactive_login)()
    try:
        await record_linkedin(result)
    except Exception as exc:  # the browser profile keeps the session even if the database is down
        print(f"Warning: could not save the LinkedIn status ({type(exc).__name__}: {exc})", file=sys.stderr)
    if result.state == linkedin_session.CONNECTED:
        print(f"connected as {result.account_name or 'unknown name'}")
        return 0
    print(f"FAILED: LinkedIn {result.state.replace('_', ' ')}: {result.detail or 'no details'}", file=sys.stderr)
    return 1


async def _job_enrich() -> dict[str, int]:
    """The targeting half of the company finder: NEW posts become ENRICHED outreaches (no drafting)."""
    from collections import Counter

    from app import scheduler
    from app.workers.pipeline import Pipeline

    async def work(db: Any) -> dict[str, int]:
        pipeline = Pipeline(db=db)
        counts: Counter[str] = Counter()
        for post in pipeline._new_posts():
            try:
                counts[await pipeline.process_post(post)] += 1
            except Exception as exc:
                db.rollback()
                counts["failed"] += 1
                print(f"  post {post.id}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"targeted": counts["targeted"], "skipped": counts["skipped"], "failed": counts["failed"]}

    return await scheduler.in_session(work)


async def _job_draft() -> int:
    """The drafter alone: ENRICHED outreaches get a first message. Returns the number of drafts."""
    from app import scheduler
    from app.workers.drafter import Drafter

    return await scheduler.in_session(lambda db: Drafter(db=db).run_once())


async def _job_draft_force(outreach_ids: list[int] | None) -> Any:
    """Discard unsent drafts (the given outreaches, or all unsent demo leads), back to ENRICHED, draft again.
    Never touches a lead with a sent or received message."""
    from app import scheduler
    from app.workers.drafter import force_redraft

    result = await scheduler.in_session(lambda db: force_redraft(db, outreach_ids))
    for outreach_id, why in result.refused:
        print(f"  refused outreach #{outreach_id}: {why}", file=sys.stderr)
    if result.refused and outreach_ids and not (result.drafted or result.discarded):
        raise CliError("nothing was regenerated")
    return {"discarded": result.discarded, "drafted": result.drafted, "refused": len(result.refused)}


def _scheduler_body(name: str, *args: Any) -> Callable[[], Awaitable[Any]]:
    """The scheduler's own job body, looked up when it runs (the scheduler import is heavy)."""

    def call() -> Awaitable[Any]:
        from app import scheduler

        return getattr(scheduler, name)(*args)

    return call


RUN_JOBS: dict[str, Callable[[], Awaitable[Any]]] = {
    "post_monitor": _scheduler_body("run_post_monitor"),
    "enrich": _job_enrich,
    "draft": _job_draft,
    "send_queue": _scheduler_body("run_send_queue", True),
    "release_held": _scheduler_body("run_send_queue", False),
    "replies": _scheduler_body("run_replies"),
    "followups": _scheduler_body("run_followups"),
    "sheets": _scheduler_body("run_sheets_sync"),
}


def _describe(result: Any) -> str:
    import dataclasses

    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        result = dataclasses.asdict(result)
    if isinstance(result, dict):
        return ", ".join(f"{key}={value}" for key, value in result.items()) or "nothing to do"
    return "nothing to do" if result is None else str(result)


async def run_job(name: str, force: bool = False, outreach_ids: list[int] | None = None) -> int:
    """Run one scheduler job body once, now, without the schedule, the failure counters or the send-window gate
    in guarded() (the workers still apply their own window, caps, hold and DRY_RUN rules). Prints the counts
    and any error; returns the exit code. `draft --force` redoes unsent drafts (see _job_draft_force)."""
    from app import sandbox, scheduler

    if (force or outreach_ids) and name != "draft":
        print("FAILED: --force and --outreach-id only apply to the draft job", file=sys.stderr)
        return 2
    if outreach_ids and not force:
        print("FAILED: --outreach-id needs --force", file=sys.stderr)
        return 2
    print(sandbox.startup_line())
    scheduler.monitor().apply_overrides()  # dashboard settings (DRY_RUN, caps ...) like a scheduled run
    try:
        result = await (_job_draft_force(outreach_ids) if force else RUN_JOBS[name]())
    except Exception as exc:
        print(f"FAILED {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"OK {name}: {_describe(result)}")
    return 0


async def run_test_llm() -> int:
    """One tiny request per provider that has a key, with its configured model. Never prints a key."""
    from app.config import settings
    from app.llm.client import llm_client
    from app.redact import redact

    providers = (
        ("groq", settings.groq_api_key, settings.groq_model),
        ("gemini", settings.gemini_api_key, settings.gemini_model),
        ("ollama", settings.ollama_api_key, settings.ollama_model),
    )
    import time

    failed = tested = 0
    for name, key, model in providers:
        if not key:
            print(f"SKIPPED {name}: no API key set")
            continue
        tested += 1
        started = time.perf_counter()
        try:
            used = await llm_client.ping(name)
        except Exception as exc:
            failed += 1
            print(f"FAIL {name} ({model}) after {time.perf_counter() - started:.1f}s: {redact(exc)}", file=sys.stderr)
        else:
            note = "" if used == model else f" [fallback: configured model {model} is retired]"
            print(f"OK {name} ({used}) {time.perf_counter() - started:.1f}s{note}")
    if not tested:
        print("No provider has an API key (GROQ_API_KEY, GEMINI_API_KEY, OLLAMA_API_KEY).", file=sys.stderr)
    return 1 if failed or not tested else 0


def main(argv: Sequence[str] | None = None, prompt: Prompt = getpass.getpass) -> int:
    from app import redact
    from app.config import warn_env_overrides

    redact.install()
    warn_env_overrides()
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="create an admin account (prompts for a password)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", help="display name (default: the part of the email before @)")
    test = commands.add_parser("test-channel", help="send one test message and list recent replies")
    test.add_argument("channel", choices=CHANNELS)
    test.add_argument("--to", required=True, help="email address (outlook) or phone number (whatsapp)")
    commands.add_parser("linkedin-login", help="sign in to LinkedIn in a visible browser (waits up to 5 minutes)")
    commands.add_parser("test-llm", help="send one tiny request to each LLM provider that has a key")
    commands.add_parser("doctor", help="table of OK/FAIL/SKIPPED for the database, channels and settings")
    job = commands.add_parser("run-job", help="run one scheduler job once, now, and print what it did")
    job.add_argument("job", choices=list(RUN_JOBS))
    job.add_argument(
        "--force",
        action="store_true",
        help="draft only: discard unsent drafts (never a sent message), return the leads to ENRICHED, draft again",
    )
    job.add_argument(
        "--outreach-id", type=int, action="append", help="with --force: only this outreach (repeatable); default is all unsent demo leads"
    )
    args = parser.parse_args(argv)

    if args.command == "test-llm":
        return asyncio.run(run_test_llm())

    if args.command == "doctor":
        from app.doctor import run_doctor

        return run_doctor()

    if args.command == "run-job":
        return asyncio.run(run_job(args.job, args.force, args.outreach_id))

    if args.command == "linkedin-login":
        return asyncio.run(run_linkedin_login())

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
