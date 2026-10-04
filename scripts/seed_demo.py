"""Seed three fake hiring posts so the whole pipeline can be exercised in DRY_RUN.

    python scripts/seed_demo.py            # seeds the configured database

The posts cover the three targeting paths:
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
from app.db.models import Post  # noqa: E402
from app.db.schemas import CompanyCreate, PostCreate  # noqa: E402

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


def main() -> None:
    if not settings.dry_run:
        raise SystemExit("Refusing to seed demo data while DRY_RUN is false.")
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        posts = seed_posts(db)
    finally:
        db.close()
    print(f"Seeded {len(posts)} demo posts:")
    for item, post in zip(DEMO_POSTS, posts):
        print(f"  #{post.id} {item['key']:14} {item['company']['name']}")
    print("Run the company finder (08:00 job) or `python -c \"import asyncio; from app.scheduler import run_company_finder as r; print(asyncio.run(r()))\"`.")


if __name__ == "__main__":
    main()
