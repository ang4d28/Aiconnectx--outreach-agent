"""Seed demo data so the whole pipeline can be exercised in DRY_RUN.

    python scripts/seed_demo.py            # seeds 3 ENRICHED email leads addressed to SEND_ALLOWLIST
    python scripts/seed_demo.py --reset    # first deletes the demo leads and their unsent messages

main() creates 3 idempotent leads (company, hiring post, contact, ENRICHED email outreach) whose contact
emails are the SEND_ALLOWLIST addresses (plus-addressed from the first one when fewer are listed), then
`python -m app.cli run-job draft` writes their drafts. It refuses to run when SEND_ALLOWLIST is empty or
DRY_RUN is false. Plus-addressed emails are not exact allowlist entries: add them to SEND_ALLOWLIST to send.

seed_posts() below is the older set of three NEW posts (reserved .test domains) used by the end-to-end test.
Its posts cover the three targeting paths:
  recruiter      the poster is a recruiter, so the poster is the contact
  employee       the poster is an employee, so the contact is found through enrichment
  email_in_post  the post contains an email address, so that address is used

Everything uses reserved .test / .invalid domains, so nothing can reach a real person.
Refuses to run unless DRY_RUN=true. The end-to-end test (backend/tests/test_e2e.py)
uses the same data.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import settings  # noqa: E402
from app.db import repository  # noqa: E402
from app.db.models import ChannelType, ContactRole, Outreach, OutreachState, Post, PostStatus  # noqa: E402
from app.db.schemas import CompanyCreate, ContactCreate, OutreachCreate, PostCreate  # noqa: E402

DEMO_POSTS: list[dict[str, Any]] = [
    {
        "key": "recruiter",
        "company": {"name": "Northwind AI", "website": None},
        "url": "https://www.linkedin.com/posts/demo-priya-nair_hiring-ml-engineers-0001",
        "poster_name": "Priya Nair",
        "poster_title": "Senior Technical Recruiter",
        "poster_profile_url": "https://www.linkedin.com/in/priya-nair-demo",
        "roles": ["ML Engineer", "Data Scientist"],
        "raw_text": "We are hiring ML engineers and data scientists. Freshers from B.Tech AI/ML welcome. DM me.",
        "relevance_score": 0.92,
    },
    {
        "key": "employee",
        "company": {"name": "Globex Labs", "website": "https://globex-labs.test"},
        "url": "https://www.linkedin.com/posts/demo-arjun-mehta_internships-0002",
        "poster_name": "Arjun Mehta",
        "poster_title": "Software Engineer",
        "poster_profile_url": "https://www.linkedin.com/in/arjun-mehta-demo",
        "roles": ["AI Intern", "Backend Engineer"],
        "raw_text": "My team at Globex Labs is hiring AI interns and backend engineers this quarter.",
        "relevance_score": 0.81,
    },
    {
        "key": "email_in_post",
        "company": {"name": "Initech Robotics", "website": "https://initech-robotics.test"},
        "url": "https://www.linkedin.com/posts/demo-sara-khan_campus-hiring-0003",
        "poster_name": "Sara Khan",
        "poster_title": "Engineering Manager",
        "poster_profile_url": "https://www.linkedin.com/in/sara-khan-demo",
        "roles": ["Computer Vision Engineer"],
        "raw_text": "Campus hiring for computer vision engineers. Send your CV to careers@initech-robotics.test",
        "relevance_score": 0.88,
    },
]


def seed_posts(db: Any) -> list[Post]:
    """Create the demo companies and posts (skipping any that already exist). Returns the posts."""
    posts: list[Post] = []
    for item in DEMO_POSTS:
        existing = repository.get_post_by_url(db, item["url"])
        if existing is not None:
            posts.append(existing)
            continue
        company = repository.get_company_by_name(db, item["company"]["name"]) or repository.create_company(
            db, CompanyCreate(**item["company"])
        )
        posts.append(
            repository.create_post(
                db,
                PostCreate(
                    url=item["url"],
                    company_id=company.id,
                    poster_name=item["poster_name"],
                    poster_title=item["poster_title"],
                    poster_profile_url=item["poster_profile_url"],
                    roles=item["roles"],
                    raw_text=item["raw_text"],
                    relevance_score=item["relevance_score"],
                ),
            )
        )
    return posts


DEMO_LEADS: list[dict[str, Any]] = [
    {
        "company": {"name": "Brightpath Analytics", "website": None},
        "url": "https://www.linkedin.com/posts/demo-meera-iyer_hiring-data-analysts-1001",
        "name": "Meera Iyer",
        "title": "Talent Acquisition Lead",
        "role": ContactRole.RECRUITER,
        "profile_url": "https://www.linkedin.com/in/meera-iyer-demo",
        "roles": ["Data Analyst Intern", "ML Engineer"],
        "raw_text": "Brightpath Analytics is hiring data analyst interns and ML engineers for our Bengaluru team. "
        "Freshers with strong Python and statistics welcome.",
    },
    {
        "company": {"name": "Lumen Robotics", "website": None},
        "url": "https://www.linkedin.com/posts/demo-rohan-das_campus-hiring-1002",
        "name": "Rohan Das",
        "title": "Head of Engineering",
        "role": ContactRole.OTHER,
        "profile_url": "https://www.linkedin.com/in/rohan-das-demo",
        "roles": ["Computer Vision Engineer", "Embedded Software Engineer"],
        "raw_text": "We are opening 6 roles for computer vision and embedded engineers at Lumen Robotics. "
        "Looking at campus hires this season, message me if you can help.",
    },
    {
        "company": {"name": "Cobalt Health Tech", "website": None},
        "url": "https://www.linkedin.com/posts/demo-nisha-rao_ai-internships-1003",
        "name": "Nisha Rao",
        "title": "HR Manager",
        "role": ContactRole.HR,
        "profile_url": "https://www.linkedin.com/in/nisha-rao-demo",
        "roles": ["AI Intern", "Backend Engineer"],
        "raw_text": "Cobalt Health Tech is hiring AI interns and backend engineers. Apply through DM or email me.",
    },
]
LEAD_COUNT = len(DEMO_LEADS)


def lead_emails(allowlist: set[str]) -> list[str]:
    """One address per demo lead, taken from the allowlist. With fewer addresses than leads the first one is
    plus-addressed (name+demo2@host), which is delivered to the same inbox."""
    base = sorted(allowlist)
    emails = base[:LEAD_COUNT]
    local, _, host = base[0].partition("@")
    local = local.split("+")[0]
    while len(emails) < LEAD_COUNT:
        emails.append(f"{local}+demo{len(emails) + 1}@{host}")
    return emails


def seed_leads(db: Any, allowlist: set[str]) -> list[tuple[Outreach, bool]]:
    """Create (or find) 3 ENRICHED email leads from a hiring post, each with its post and company, addressed to
    the allowlist. Safe to run again: nothing is duplicated. Returns (outreach, on_allowlist) pairs."""
    if not allowlist:
        raise SystemExit("Refusing to seed demo leads: SEND_ALLOWLIST is empty, so the contacts could be real.")
    results: list[tuple[Outreach, bool]] = []
    for item, email in zip(DEMO_LEADS, lead_emails(allowlist)):
        company = repository.get_company_by_name(db, item["company"]["name"]) or repository.create_company(
            db, CompanyCreate(**item["company"])
        )
        post = repository.get_post_by_url(db, item["url"]) or repository.create_post(
            db,
            PostCreate(
                url=item["url"],
                company_id=company.id,
                poster_name=item["name"],
                poster_title=item["title"],
                poster_profile_url=item["profile_url"],
                roles=item["roles"],
                raw_text=item["raw_text"],
                relevance_score=0.9,
                status=PostStatus.PROCESSED,  # already targeted: the enrich job must not pick it up again
            ),
        )
        contact = repository.get_contact_by_email(db, email) or repository.create_contact(
            db,
            ContactCreate(
                company_id=company.id, name=item["name"], email=email, title=item["title"], role=item["role"]
            ),
        )
        outreach = next(iter(repository.get_outreaches_by_contact(db, contact.id, limit=1)), None)
        if outreach is None:
            outreach = repository.create_outreach(
                db,
                OutreachCreate(
                    contact_id=contact.id, post_id=post.id, channel=ChannelType.EMAIL, state=OutreachState.ENRICHED
                ),
            )
        results.append((outreach, email.lower() in allowlist))
    return results


def reset_leads(db: Any) -> tuple[int, list[str]]:
    """Delete the demo leads (their post, contact, outreaches, unsent messages and attention items, and the
    company when nothing else uses it). A lead with any sent or received message is left alone. Only the posts
    in DEMO_LEADS are looked at, so real leads are never touched. Returns (deleted, skipped reasons)."""
    from sqlalchemy import select

    from app.db.models import AttentionItem, AttentionStatus, Company, Contact, Message, Post
    from app.demo import is_demo_post_url
    from app.workers.drafter import reset_refusal

    deleted, skipped = 0, []
    for item in DEMO_LEADS:
        post = repository.get_post_by_url(db, item["url"])
        if post is None or not is_demo_post_url(post.url):
            continue
        outreaches = list(db.scalars(select(Outreach).where(Outreach.post_id == post.id)))
        why = reset_refusal(db, outreaches) if outreaches else None
        if why:
            skipped.append(f"{item['name']} ({item['company']['name']}): {why}")
            continue
        outreach_ids = {o.id for o in outreaches}
        message_ids = {m for m in db.scalars(select(Message.id).where(Message.outreach_id.in_(outreach_ids)))}
        for attention in db.scalars(select(AttentionItem).where(AttentionItem.status == AttentionStatus.OPEN)):
            payload = attention.payload or {}
            if payload.get("outreach_id") in outreach_ids or payload.get("message_id") in message_ids:
                db.delete(attention)
        for message in db.scalars(select(Message).where(Message.outreach_id.in_(outreach_ids))):
            db.delete(message)
        contact_ids = {o.contact_id for o in outreaches}
        for outreach in outreaches:
            db.delete(outreach)
        db.flush()
        for contact_id in contact_ids:
            if db.scalar(select(Outreach.id).where(Outreach.contact_id == contact_id).limit(1)) is None:
                contact = db.get(Contact, contact_id)
                if contact is not None:
                    db.delete(contact)
        company_id = post.company_id
        db.delete(post)
        db.flush()
        if company_id is not None:
            in_use = db.scalar(select(Contact.id).where(Contact.company_id == company_id).limit(1)) or db.scalar(
                select(Post.id).where(Post.company_id == company_id).limit(1)
            )
            company = db.get(Company, company_id)
            if company is not None and in_use is None:
                db.delete(company)
        db.commit()
        deleted += 1
    return deleted, skipped


def main(argv: list[str] | None = None) -> None:
    import argparse

    from app import sandbox

    parser = argparse.ArgumentParser(description="Seed three demo leads (ENRICHED, email) addressed to SEND_ALLOWLIST.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="first delete the demo leads and their unsent messages (never one with a sent message), then recreate them",
    )
    args = parser.parse_args(argv)
    if not settings.dry_run:
        raise SystemExit("Refusing to seed demo data while DRY_RUN is false.")
    allowlist = sandbox.allowlist()
    if not allowlist:
        raise SystemExit("Refusing to seed demo leads: SEND_ALLOWLIST is empty. Set it to your own address(es).")
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        if args.reset:
            deleted, skipped = reset_leads(db)
            print(f"Reset: deleted {deleted} demo lead(s).")
            for reason in skipped:
                print(f"  kept {reason}")
        leads = seed_leads(db, allowlist)
        emails = [repository.get_contact(db, outreach.contact_id).email for outreach, _ in leads]
    finally:
        db.close()
    print(f"Seeded {len(leads)} demo leads (ENRICHED, email, source post):")
    for (outreach, listed), item, email in zip(leads, DEMO_LEADS, emails):
        note = "" if listed else "  (not on SEND_ALLOWLIST: add it, or this one is blocked)"
        print(f"  outreach #{outreach.id} {item['company']['name']:22} {email}{note}")
    print("Next: python -m app.cli run-job draft")


if __name__ == "__main__":
    main()
