"""What marks a lead as demo data (scripts/seed_demo.py creates them, the CLI and the seed reset find them)."""

from __future__ import annotations

# Every demo hiring post URL contains this; real posts never do.
DEMO_POST_MARKER = "/posts/demo-"


def is_demo_post_url(url: str | None) -> bool:
    return bool(url) and DEMO_POST_MARKER in url
