from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models import PostStatus
from app.llm.prompts import PostExtractionResult
from app.workers.post_monitor import PostMonitor


@pytest.fixture
def mock_db():
    """Mock database session."""
    return MagicMock()


@pytest.fixture
def mock_ddg_response():
    """Mock DuckDuckGo search response."""
    return [
        {
            "href": "https://linkedin.com/posts/ddg-1",
            "title": "AI Job Opening",
            "body": "Looking for AI graduates",
        },
        {
            "href": "https://linkedin.com/posts/ddg-2",
            "title": "Campus Recruitment",
            "body": "Campus hiring for tech roles",
        },
    ]


@pytest.fixture
def mock_extraction():
    """Mock LLM extraction result."""
    return PostExtractionResult(
        is_hiring_post=True,
        company="TechCorp",
        roles=["AI Engineer", "ML Engineer"],
        poster_name="Jane Doe",
        poster_title="HR Manager",
        poster_type="recruiter",
        contact_email_in_post="jane@techcorp.com",
        apply_link="https://techcorp.com/careers",
        relevance_score=0.85,
    )


class TestPostMonitor:
    """Test PostMonitor functionality."""

    @pytest.mark.asyncio
    async def test_search_duckduckgo(self, mock_db, mock_ddg_response):
        """Test results are mapped from the DuckDuckGo library."""
        monitor = PostMonitor(db=mock_db)

        with patch("app.workers.post_monitor.DDGS") as mock_ddgs_class:
            mock_ddgs = MagicMock()
            mock_ddgs_class.return_value.__enter__.return_value = mock_ddgs
            mock_ddgs.text.return_value = mock_ddg_response

            results = await monitor._search("test query")

            assert len(results) == 2
            assert results[0]["url"] == "https://linkedin.com/posts/ddg-1"
            assert results[1]["title"] == "Campus Recruitment"

    @pytest.mark.asyncio
    async def test_search_retries_rate_limit_then_succeeds(self, mock_db, mock_ddg_response):
        """A rate-limited call is retried with backoff and the next attempt's results are used."""
        monitor = PostMonitor(db=mock_db)
        monitor.retry_base_delay = 0

        with patch("app.workers.post_monitor.DDGS") as mock_ddgs_class:
            mock_ddgs = MagicMock()
            mock_ddgs_class.return_value.__enter__.return_value = mock_ddgs
            mock_ddgs.text.side_effect = [Exception("202 Ratelimit"), mock_ddg_response]

            results = await monitor._search("test query")

            assert len(results) == 2
            assert mock_ddgs.text.call_count == 2

    @pytest.mark.asyncio
    async def test_search_skips_query_after_retries_exhausted(self, mock_db):
        """When every attempt fails the query is skipped (no results, no exception)."""
        monitor = PostMonitor(db=mock_db)
        monitor.retry_base_delay = 0

        with patch("app.workers.post_monitor.DDGS") as mock_ddgs_class:
            mock_ddgs = MagicMock()
            mock_ddgs_class.return_value.__enter__.return_value = mock_ddgs
            mock_ddgs.text.side_effect = Exception("202 Ratelimit")

            results = await monitor._search("test query")

            assert results == []
            assert mock_ddgs.text.call_count == monitor.max_attempts

    def test_dedupe_by_url(self, mock_db):
        """Test URL deduplication."""
        monitor = PostMonitor(db=mock_db)
        
        results = [
            {"url": "https://example.com/post1"},
            {"url": "https://example.com/post1?ref=twitter"},  # Same normalized URL
            {"url": "https://example.com/post2"},
            {"url": ""},  # Empty URL
        ]
        
        deduped = monitor._dedupe_by_url(results)
        
        assert len(deduped) == 2
        assert deduped[0]["url"] == "https://example.com/post1"
        assert deduped[1]["url"] == "https://example.com/post2"

    def test_filter_by_age_recent(self, mock_db):
        """Test age filter keeps recent posts."""
        monitor = PostMonitor(db=mock_db)
        monitor.max_age_hours = 48
        
        recent = datetime.utcnow() - timedelta(hours=24)
        results = [
            {"url": "https://example.com/post1", "published": recent.isoformat()},
            {"url": "https://example.com/post2", "published": None},  # No date
        ]
        
        filtered = monitor._filter_by_age(results)
        
        assert len(filtered) == 2

    def test_filter_by_age_old(self, mock_db):
        """Test age filter removes old posts."""
        monitor = PostMonitor(db=mock_db)
        monitor.max_age_hours = 48
        
        old = datetime.utcnow() - timedelta(hours=72)
        recent = datetime.utcnow() - timedelta(hours=12)
        
        results = [
            {"url": "https://example.com/post1", "published": old.isoformat()},
            {"url": "https://example.com/post2", "published": recent.isoformat()},
        ]
        
        filtered = monitor._filter_by_age(results)
        
        assert len(filtered) == 1
        assert filtered[0]["url"] == "https://example.com/post2"

    @pytest.mark.asyncio
    async def test_extract_and_validate_success(self, mock_db, mock_extraction):
        """Test successful extraction and validation."""
        monitor = PostMonitor(db=mock_db)
        
        result = {
            "url": "https://linkedin.com/posts/test",
            "title": "Hiring AI Engineers",
            "content": "We're looking for AI engineers",
        }
        
        with patch("app.workers.post_monitor.llm_client") as mock_llm:
            mock_llm.complete_json = AsyncMock(return_value=mock_extraction)
            
            post_data = await monitor._extract_and_validate(result)
            
            assert post_data is not None
            assert post_data.url == "https://linkedin.com/posts/test"
            assert post_data.relevance_score == 0.85
            assert post_data.poster_name == "Jane Doe"
            assert len(post_data.roles) == 2
            assert post_data.status == PostStatus.NEW

    @pytest.mark.asyncio
    async def test_extract_and_validate_not_hiring(self, mock_db):
        """Test filtering non-hiring posts."""
        monitor = PostMonitor(db=mock_db)
        
        result = {
            "url": "https://linkedin.com/posts/test",
            "title": "Random Post",
            "content": "Just sharing some thoughts",
        }
        
        non_hiring_extraction = PostExtractionResult(
            is_hiring_post=False,
            company=None,
            roles=[],
            poster_name="John Doe",
            poster_title="Engineer",
            poster_type="employee",
            contact_email_in_post=None,
            apply_link=None,
            relevance_score=0.2,
        )
        
        with patch("app.workers.post_monitor.llm_client") as mock_llm:
            mock_llm.complete_json = AsyncMock(return_value=non_hiring_extraction)
            
            post_data = await monitor._extract_and_validate(result)
            
            assert post_data is None

    @pytest.mark.asyncio
    async def test_extract_and_validate_low_score(self, mock_db):
        """Test filtering low relevance posts."""
        monitor = PostMonitor(db=mock_db)
        
        result = {
            "url": "https://linkedin.com/posts/test",
            "title": "Hiring",
            "content": "Looking for sales people",
        }
        
        low_score_extraction = PostExtractionResult(
            is_hiring_post=True,
            company="SalesCo",
            roles=["Sales Rep"],
            poster_name="Bob Smith",
            poster_title="Sales Manager",
            poster_type="recruiter",
            contact_email_in_post=None,
            apply_link=None,
            relevance_score=0.3,  # Below 0.6 threshold
        )
        
        with patch("app.workers.post_monitor.llm_client") as mock_llm:
            mock_llm.complete_json = AsyncMock(return_value=low_score_extraction)
            
            post_data = await monitor._extract_and_validate(result)
            
            assert post_data is None

    @pytest.mark.asyncio
    async def test_run_once_full_cycle(self, mock_db, mock_ddg_response, mock_extraction):
        """Test full run_once cycle."""
        monitor = PostMonitor(db=mock_db)

        with patch("app.workers.post_monitor.DDGS") as mock_ddgs_class:
            mock_ddgs = MagicMock()
            mock_ddgs_class.return_value.__enter__.return_value = mock_ddgs
            mock_ddgs.text.return_value = mock_ddg_response

            with patch("app.workers.post_monitor.llm_client") as mock_llm:
                mock_llm.complete_json = AsyncMock(return_value=mock_extraction)

                with patch("app.workers.post_monitor.repository") as mock_repo:
                    mock_repo.get_post_by_url.return_value = None  # No duplicates
                    mock_repo.create_post.return_value = MagicMock()

                    with patch.object(monitor, "_load_queries") as mock_load:
                        mock_load.return_value = ["test query"]

                        result = await monitor.run_once()

                        assert result["total_results"] == 2
                        assert result["saved"] >= 0
                        assert "processed" in result

    def test_load_queries_default(self, mock_db):
        """Test loading default queries when file doesn't exist."""
        monitor = PostMonitor(db=mock_db)
        
        with patch("pathlib.Path.exists") as mock_exists:
            mock_exists.return_value = False
            
            queries = monitor._load_queries()
            
            assert len(queries) > 0
            assert "linkedin.com/posts" in queries[0]

    def test_load_queries_from_yaml(self, mock_db):
        """Test loading queries from YAML file."""
        monitor = PostMonitor(db=mock_db)
        
        mock_yaml_data = {
            "queries": [
                "custom query 1",
                "custom query 2",
            ]
        }
        
        with patch("pathlib.Path.exists") as mock_exists:
            mock_exists.return_value = True
            
            with patch("builtins.open", MagicMock()):
                with patch("yaml.safe_load") as mock_yaml:
                    mock_yaml.return_value = mock_yaml_data
                    
                    queries = monitor._load_queries()
                    
                    assert len(queries) == 2
                    assert queries[0] == "custom query 1"
                    assert queries[1] == "custom query 2"
