from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models import Company, ContactRole
from app.llm.prompts import CompanyClassificationResult
from app.workers.enrichment import EnrichmentWorker


@pytest.fixture
def mock_db():
    """Mock database session."""
    return MagicMock()


@pytest.fixture
def mock_company():
    """Mock company object."""
    company = MagicMock(spec=Company)
    company.id = 1
    company.name = "TechCorp"
    company.website = "techcorp.com"
    company.linkedin_url = "https://linkedin.com/company/techcorp"
    company.industry = "Technology"
    company.size = None
    return company


@pytest.fixture
def mock_classification():
    """Mock company classification result."""
    return CompanyClassificationResult(
        company_type="mid_tier",
        employee_count_estimate=200,
        reasoning="Established tech company with professional HR",
    )


class TestEnrichmentWorker:
    """Test EnrichmentWorker functionality."""

    @pytest.mark.asyncio
    async def test_classify_company(self, mock_db, mock_company, mock_classification):
        """Test company classification."""
        worker = EnrichmentWorker(db=mock_db)
        
        with patch("app.workers.enrichment.llm_client") as mock_llm:
            mock_llm.complete_json = AsyncMock(return_value=mock_classification)
            
            company_type = await worker._classify_company(mock_company)
            
            assert company_type == "mid_tier"
            mock_llm.complete_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_find_linkedin_profile_duckduckgo(self, mock_db):
        """Test LinkedIn profile search via DuckDuckGo."""
        worker = EnrichmentWorker(db=mock_db)

        mock_search_results = [
            {"href": "https://linkedin.com/in/john-doe-hr", "title": "John Doe - HR Manager at TechCorp"},
            {"href": "https://techcorp.com/team", "title": "Team page"},
        ]

        with patch("duckduckgo_search.DDGS") as mock_ddgs_class:
            mock_ddgs = MagicMock()
            mock_ddgs_class.return_value.__enter__.return_value = mock_ddgs
            mock_ddgs.text.return_value = mock_search_results

            linkedin_url = await worker._find_linkedin_profile("TechCorp", ContactRole.HR)

            assert linkedin_url == "https://linkedin.com/in/john-doe-hr"

    @pytest.mark.asyncio
    async def test_scrape_company_emails(self, mock_db):
        """Test email scraping from company website."""
        worker = EnrichmentWorker(db=mock_db)
        
        mock_html = """
        <html>
        <body>
            <p>Contact us at hr@techcorp.com</p>
            <p>Support: support@techcorp.com</p>
            <p>Sales: john.doe@techcorp.com</p>
        </body>
        </html>
        """
        
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = mock_html
            mock_client.get.return_value = mock_response
            
            emails = await worker._scrape_company_emails("https://techcorp.com")
            
            # Should filter out support@ but keep others
            assert len(emails) >= 1
            assert any("techcorp.com" in email for email in emails)

    def test_generate_email_patterns(self, mock_db):
        """Test email pattern generation."""
        worker = EnrichmentWorker(db=mock_db)
        
        patterns = worker._generate_email_patterns("John Doe", "techcorp.com")
        
        assert "john@techcorp.com" in patterns
        assert "john.doe@techcorp.com" in patterns
        assert "jdoe@techcorp.com" in patterns
        assert len(patterns) >= 3

    def test_generate_email_patterns_single_name(self, mock_db):
        """Test email pattern generation with single name."""
        worker = EnrichmentWorker(db=mock_db)
        
        patterns = worker._generate_email_patterns("John", "techcorp.com")
        
        assert len(patterns) == 0  # Single name should not generate patterns

    @pytest.mark.asyncio
    async def test_verify_email_no_mx(self, mock_db):
        """Test email verification with no MX records."""
        worker = EnrichmentWorker(db=mock_db)
        
        with patch("socket.getaddrinfo") as mock_socket:
            mock_socket.side_effect = Exception("No MX records")
            
            confidence = await worker._verify_email("test@invalid-domain.xyz")
            
            assert confidence == 0.0

    @pytest.mark.asyncio
    async def test_verify_email_mx_only(self, mock_db):
        """Test email verification with MX records only."""
        worker = EnrichmentWorker(db=mock_db)
        
        with patch("socket.getaddrinfo") as mock_socket:
            mock_socket.return_value = [("mx.example.com", 25)]
            
            with patch("smtplib.SMTP") as mock_smtp_class:
                mock_smtp = MagicMock()
                mock_smtp_class.return_value.__enter__.return_value = mock_smtp
                mock_smtp.rcpt.side_effect = Exception("SMTP failed")
                
                confidence = await worker._verify_email("test@example.com")
                
                # Should have MX confidence but SMTP failed
                assert confidence == 0.5

    def test_extract_name_from_linkedin_url(self, mock_db):
        """Test name extraction from LinkedIn URL."""
        worker = EnrichmentWorker(db=mock_db)
        
        url = "https://linkedin.com/in/john-doe-123"
        name = worker._extract_name_from_linkedin_url(url)
        
        assert name == "John Doe"

    def test_extract_name_from_linkedin_url_no_numbers(self, mock_db):
        """Test name extraction without trailing numbers."""
        worker = EnrichmentWorker(db=mock_db)
        
        url = "https://linkedin.com/in/jane-smith"
        name = worker._extract_name_from_linkedin_url(url)
        
        assert name == "Jane Smith"

    def test_role_to_search_keywords(self, mock_db):
        """Test role to search keywords conversion."""
        worker = EnrichmentWorker(db=mock_db)
        
        keywords = worker._role_to_search_keywords(ContactRole.HR)
        assert "HR Manager" in keywords or "Human Resources" in keywords
        
        keywords = worker._role_to_search_keywords(ContactRole.FOUNDER)
        assert "Founder" in keywords or "CEO" in keywords
        
        keywords = worker._role_to_search_keywords(ContactRole.MD)
        assert "Managing Director" in keywords or "MD" in keywords

    def test_role_to_title(self, mock_db):
        """Test role to title conversion."""
        worker = EnrichmentWorker(db=mock_db)
        
        assert worker._role_to_title(ContactRole.HR) == "HR Manager"
        assert worker._role_to_title(ContactRole.FOUNDER) == "Founder"
        assert worker._role_to_title(ContactRole.MD) == "Managing Director"
        assert worker._role_to_title(ContactRole.RECRUITER) == "Recruiter"

    @pytest.mark.asyncio
    async def test_enrich_company_full(self, mock_db, mock_company, mock_classification):
        """Test full company enrichment cycle."""
        worker = EnrichmentWorker(db=mock_db)
        
        with patch("app.workers.enrichment.llm_client") as mock_llm:
            mock_llm.complete_json = AsyncMock(return_value=mock_classification)
            
            with patch("app.workers.enrichment.repository") as mock_repo:
                mock_repo.update_company.return_value = mock_company
                
                result = await worker.enrich_company(mock_company)
                
                assert result["company_id"] == 1
                assert result["company_name"] == "TechCorp"
                assert result["company_type"] == "mid_tier"
                mock_repo.update_company.assert_called_once()

    @pytest.mark.asyncio
    async def test_find_contact_success(self, mock_db, mock_company):
        """Test successful contact finding."""
        worker = EnrichmentWorker(db=mock_db)
        
        with patch.object(worker, "_find_linkedin_profile") as mock_linkedin:
            mock_linkedin.return_value = "https://linkedin.com/in/john-hr"
            
            with patch.object(worker, "_find_email") as mock_email:
                mock_email.return_value = ("john@techcorp.com", 0.9)
                
                with patch("app.workers.enrichment.repository") as mock_repo:
                    mock_contact = MagicMock()
                    mock_contact.id = 1
                    mock_contact.name = "John Hr"
                    mock_contact.email = "john@techcorp.com"
                    mock_repo.create_contact.return_value = mock_contact
                    
                    contact = await worker.find_contact(mock_company, ContactRole.HR)
                    
                    assert contact is not None
                    assert contact.email == "john@techcorp.com"

    @pytest.mark.asyncio
    async def test_find_contact_low_confidence(self, mock_db, mock_company):
        """Test contact finding with low email confidence."""
        worker = EnrichmentWorker(db=mock_db)
        
        with patch.object(worker, "_find_linkedin_profile") as mock_linkedin:
            mock_linkedin.return_value = "https://linkedin.com/in/john-hr"
            
            with patch.object(worker, "_find_email") as mock_email:
                mock_email.return_value = ("john@techcorp.com", 0.3)  # Low confidence
                
                contact = await worker.find_contact(mock_company, ContactRole.HR)
                
                assert contact is None

    @pytest.mark.asyncio
    async def test_find_contact_no_linkedin(self, mock_db, mock_company):
        """Test contact finding when LinkedIn profile not found."""
        worker = EnrichmentWorker(db=mock_db)
        
        with patch.object(worker, "_find_linkedin_profile") as mock_linkedin:
            mock_linkedin.return_value = None
            
            contact = await worker.find_contact(mock_company, ContactRole.HR)
            
            assert contact is None
