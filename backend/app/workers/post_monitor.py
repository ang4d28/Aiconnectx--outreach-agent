from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from sqlalchemy.orm import Session

from app.db import SessionLocal, repository
from app.db.models import PostStatus
from app.db.schemas import CompanyCreate, PostCreate
from app.llm.client import llm_client
from app.llm.prompts import PostExtractionResult, extract_post_prompt

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

logger = logging.getLogger(__name__)

DEFAULT_QUERIES = [
    'site:linkedin.com/posts ("hiring" OR "campus hiring" OR "internship" OR "freshers") AI ML',
    'site:linkedin.com/posts "B.Tech" ("placement" OR "recruiting") AI "machine learning"',
    'site:linkedin.com/posts ("AI engineer" OR "ML engineer") "fresher" OR "graduate"',
]

QUERIES_FILE = Path(__file__).parent.parent.parent / "search_queries.yaml"


class PostMonitor:
    def __init__(self, db: Session | None = None):
        self.db = db or SessionLocal()
        self.max_attempts = 3
        self.retry_base_delay = 5.0
        self.max_age_hours = 48

    async def run_once(self) -> dict[str, Any]:
        """Run one cycle of post discovery."""
        logger.info("Starting post monitor cycle")
        
        queries = self._load_queries()
        all_results = []
        
        for query in queries:
            try:
                results = await self._search(query)
                all_results.extend(results)
            except Exception as e:
                logger.error(f"Search failed for query '{query}': {e}")
        
        deduped = self._dedupe_by_url(all_results)
        filtered = self._filter_by_age(deduped)
        
        logger.info(f"Found {len(all_results)} results, {len(deduped)} unique, {len(filtered)} within {self.max_age_hours}h")
        
        processed = 0
        saved = 0
        skipped = 0
        
        for result in filtered:
            url = result.get("url", "")
            
            # Skip if already in database
            if repository.get_post_by_url(self.db, url):
                skipped += 1
                continue
            
            try:
                post_data = await self._extract_and_validate(result)
                if post_data:
                    repository.create_post(self.db, post_data)
                    saved += 1
                    logger.info(f"Saved post: {url} (score={post_data.relevance_score})")
                else:
                    skipped += 1
                    
                processed += 1
                
            except Exception as e:
                logger.error(f"Failed to process {url}: {e}")
                skipped += 1
        
        summary = {
            "total_results": len(all_results),
            "unique_results": len(deduped),
            "within_age": len(filtered),
            "processed": processed,
            "saved": saved,
            "skipped": skipped,
        }
        
        logger.info(f"Post monitor cycle complete: {summary}")
        return summary

    async def _search(self, query: str) -> list[dict[str, Any]]:
        """Search DuckDuckGo, retrying with backoff on failure; skip the query once retries run out."""
        if DDGS is None:
            logger.error("duckduckgo-search library not installed")
            return []

        for attempt in range(1, self.max_attempts + 1):
            try:
                return await asyncio.to_thread(self._ddg_text, query)
            except Exception as e:
                if attempt == self.max_attempts:
                    logger.error(f"DuckDuckGo search failed for '{query}' after {attempt} attempts, skipping: {e}")
                    return []
                delay = self.retry_base_delay * 2 ** (attempt - 1)
                logger.warning(f"DuckDuckGo search failed (attempt {attempt}/{self.max_attempts}): {e}; retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
        return []

    def _ddg_text(self, query: str) -> list[dict[str, Any]]:
        with DDGS() as ddgs:
            items = ddgs.text(query, max_results=20)
            return [
                {
                    "url": item.get("href", ""),
                    "title": item.get("title", ""),
                    "content": item.get("body", ""),
                    "published": None,  # DuckDuckGo doesn't provide dates
                }
                for item in items
            ]

    def _dedupe_by_url(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate URLs."""
        seen = set()
        deduped = []
        
        for result in results:
            url = result.get("url", "")
            if not url:
                continue
            
            # Normalize URL
            parsed = urlparse(url)
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            
            if normalized not in seen:
                seen.add(normalized)
                result["url"] = normalized
                deduped.append(result)
        
        return deduped

    def _filter_by_age(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter out posts older than max_age_hours."""
        cutoff = datetime.utcnow() - timedelta(hours=self.max_age_hours)
        filtered = []
        
        for result in results:
            published = result.get("published")
            
            # If no date available, include it (DuckDuckGo case)
            if not published:
                filtered.append(result)
                continue
            
            try:
                if isinstance(published, str):
                    pub_date = datetime.fromisoformat(published.replace("Z", "+00:00"))
                elif isinstance(published, datetime):
                    pub_date = published
                else:
                    filtered.append(result)
                    continue
                
                if pub_date >= cutoff:
                    filtered.append(result)
                    
            except Exception as e:
                logger.warning(f"Failed to parse date {published}: {e}")
                filtered.append(result)
        
        return filtered

    async def _extract_and_validate(
        self, result: dict[str, Any]
    ) -> PostCreate | None:
        """Extract post data using LLM and validate."""
        url = result.get("url", "")
        title = result.get("title", "")
        content = result.get("content", "")
        
        post_text = f"{title}\n\n{content}"
        
        try:
            prompt = extract_post_prompt(post_text)
            extraction: PostExtractionResult = await llm_client.complete_json(
                prompt, PostExtractionResult
            )
            
            # Filter: must be hiring post and relevance >= 0.6
            if not extraction.is_hiring_post or extraction.relevance_score < 0.6:
                logger.debug(
                    f"Skipping post {url}: is_hiring={extraction.is_hiring_post}, "
                    f"score={extraction.relevance_score}"
                )
                return None
            
            # Create post data
            post_data = PostCreate(
                url=url,
                company_id=self._resolve_company(extraction.company),
                poster_name=extraction.poster_name,
                poster_profile_url=None,
                poster_title=extraction.poster_title,
                roles=extraction.roles,
                raw_text=post_text,
                relevance_score=extraction.relevance_score,
                status=PostStatus.NEW,
            )
            
            return post_data
            
        except Exception as e:
            logger.error(f"LLM extraction failed for {url}: {e}")
            return None

    def _resolve_company(self, name: str | None) -> int | None:
        """Find or create the company named in a post so targeting can pick a contact."""
        name = (name or "").strip()
        if not name:
            return None
        try:
            company = repository.get_company_by_name(self.db, name) or repository.create_company(
                self.db, CompanyCreate(name=name[:255])
            )
        except Exception as e:
            logger.error(f"Could not save company '{name}': {e}")
            self.db.rollback()
            return None
        return company.id if isinstance(company.id, int) else None

    def _stored_queries(self) -> list[str] | None:
        """Queries edited on the dashboard (settings table) win over the YAML file."""
        try:
            row = repository.get_setting(self.db, "search_queries")
            raw = getattr(row, "value", None)
            queries = json.loads(raw) if isinstance(raw, str) else None
        except Exception as e:
            logger.warning(f"Could not read stored search queries: {e}")
            return None
        if isinstance(queries, list) and queries and all(isinstance(q, str) for q in queries):
            return queries
        return None

    def _load_queries(self) -> list[str]:
        """Load search queries (dashboard setting, else YAML file, else defaults)."""
        stored = self._stored_queries()
        if stored:
            return stored
        if not QUERIES_FILE.exists():
            logger.info(f"No queries file found at {QUERIES_FILE}, using defaults")
            return DEFAULT_QUERIES
        
        try:
            with open(QUERIES_FILE, "r") as f:
                data = yaml.safe_load(f)
            
            queries = data.get("queries", [])
            if not queries:
                logger.warning("Empty queries in YAML, using defaults")
                return DEFAULT_QUERIES
            
            return queries
            
        except Exception as e:
            logger.error(f"Failed to load queries from {QUERIES_FILE}: {e}")
            return DEFAULT_QUERIES


async def run_once() -> dict[str, Any]:
    """CLI entry point for running post monitor once."""
    monitor = PostMonitor()
    try:
        return await monitor.run_once()
    finally:
        monitor.db.close()


def main() -> None:
    """CLI command entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    logger.info("Running post monitor...")
    result = asyncio.run(run_once())
    
    print("\n=== Post Monitor Summary ===")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
