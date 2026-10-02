from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models import Company, Contact, ContactRole, Post
from app.workers.targeting import TargetingWorker


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
    company.size = "mid_tier"
    return company


@pytest.fixture
def mock_post():
    """Mock post object."""
    post = MagicMock(spec=Post)
    post.id = 1
    post.company_id = 1
    post.url = "https://linkedin.com/posts/test"
    post.poster_name = "Jane Doe"
    post.poster_title = "HR Manager"
    post.poster_profile_url = "https://linkedin.com/in/jane-doe"
    post.raw_text = "We're hiring! Contact us."
    return post


@pytest.fixture
def mock_contact():
    """Mock contact object."""
    contact = MagicMock(spec=Contact)
    contact.id = 1
    contact.company_id = 1
    contact.name = "Jane Doe"
    contact.email = "jane@techcorp.com"
    contact.linkedin_url = "https://linkedin.com/in/jane-doe"
    contact.role = ContactRole.HR
    return contact


class TestTargetingWorker:
    """Test TargetingWorker functionality."""

    @pytest.mark.asyncio
    async def test_pick_target_with_email_in_post(self, mock_db, mock_post):
        """Test targeting when email is present in post."""
        worker = TargetingWorker(db=mock_db)
        mock_post.raw_text = "Contact us at hiring@techcorp.com for opportunities"
        
        with patch.object(worker, "_create_direct_contact") as mock_create:
            mock_create.return_value = MagicMock()
            
            contact = await worker.pick_target(mock_post)
            
            mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_pick_target_recruiter_poster(self, mock_db, mock_post, mock_contact):
        """Test targeting recruiter poster."""
        worker = TargetingWorker(db=mock_db)
        mock_post.poster_title = "Senior Recruiter"
        
        with patch.object(worker, "_target_poster") as mock_target:
            mock_target.return_value = mock_contact
            
            contact = await worker.pick_target(mock_post)
            
            assert contact is not None
            mock_target.assert_called_once()

    @pytest.mark.asyncio
    async def test_pick_target_founder_poster(self, mock_db, mock_post, mock_contact):
        """Test targeting founder poster."""
        worker = TargetingWorker(db=mock_db)
        mock_post.poster_title = "Co-Founder & CEO"
        
        with patch.object(worker, "_target_poster") as mock_target:
            mock_target.return_value = mock_contact
            
            contact = await worker.pick_target(mock_post)
            
            assert contact is not None
            mock_target.assert_called_once()

    @pytest.mark.asyncio
    async def test_pick_target_employee_poster(self, mock_db, mock_post, mock_contact):
        """Test targeting via enrichment for employee poster."""
        worker = TargetingWorker(db=mock_db)
        mock_post.poster_title = "Software Engineer"
        
        with patch.object(worker, "_target_via_enrichment") as mock_enrich:
            mock_enrich.return_value = mock_contact
            
            contact = await worker.pick_target(mock_post)
            
            assert contact is not None
            mock_enrich.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_direct_contact(self, mock_db, mock_post):
        """Test creating direct contact with email."""
        worker = TargetingWorker(db=mock_db)
        
        with patch("app.workers.targeting.repository") as mock_repo:
            mock_repo.get_contact_by_email.return_value = None
            mock_repo.create_contact.return_value = MagicMock()
            
            contact = await worker._create_direct_contact(
                mock_post, "test@techcorp.com"
            )
            
            assert contact is not None
            mock_repo.create_contact.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_direct_contact_existing(self, mock_db, mock_post, mock_contact):
        """Test creating direct contact when already exists."""
        worker = TargetingWorker(db=mock_db)
        
        with patch("app.workers.targeting.repository") as mock_repo:
            mock_repo.get_contact_by_email.return_value = mock_contact
            
            contact = await worker._create_direct_contact(
                mock_post, "jane@techcorp.com"
            )
            
            assert contact == mock_contact
            mock_repo.create_contact.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_poster(self, mock_db, mock_post, mock_company):
        """Test targeting the poster directly."""
        worker = TargetingWorker(db=mock_db)
        
        with patch("app.workers.targeting.repository") as mock_repo:
            mock_repo.get_contacts_by_company.return_value = []
            mock_repo.create_contact.return_value = MagicMock()
            mock_repo.get_company.return_value = mock_company
            
            with patch.object(worker.enrichment, "_find_email") as mock_email:
                mock_email.return_value = ("jane@techcorp.com", 0.9)
                
                contact = await worker._target_poster(mock_post)
                
                assert contact is not None
                mock_repo.create_contact.assert_called_once()

    @pytest.mark.asyncio
    async def test_target_poster_existing(self, mock_db, mock_post, mock_contact):
        """Test targeting poster when contact already exists."""
        worker = TargetingWorker(db=mock_db)
        
        with patch("app.workers.targeting.repository") as mock_repo:
            mock_contact.linkedin_url = mock_post.poster_profile_url
            mock_repo.get_contacts_by_company.return_value = [mock_contact]
            
            contact = await worker._target_poster(mock_post)
            
            assert contact == mock_contact
            mock_repo.create_contact.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_via_enrichment_startup(self, mock_db, mock_post, mock_company, mock_contact):
        """Test targeting via enrichment for startup."""
        worker = TargetingWorker(db=mock_db)
        mock_company.size = "startup"
        
        with patch("app.workers.targeting.repository") as mock_repo:
            mock_repo.get_company.return_value = mock_company
            
            with patch.object(worker.enrichment, "find_contact") as mock_find:
                mock_find.return_value = mock_contact
                
                contact = await worker._target_via_enrichment(mock_post)
                
                assert contact is not None
                # Should target founder for startup
                call_args = mock_find.call_args
                assert call_args[0][1] == ContactRole.FOUNDER

    @pytest.mark.asyncio
    async def test_target_via_enrichment_corporate(self, mock_db, mock_post, mock_company, mock_contact):
        """Test targeting via enrichment for corporate."""
        worker = TargetingWorker(db=mock_db)
        mock_company.size = "corporate"
        
        with patch("app.workers.targeting.repository") as mock_repo:
            mock_repo.get_company.return_value = mock_company
            
            with patch.object(worker.enrichment, "find_contact") as mock_find:
                mock_find.return_value = mock_contact
                
                contact = await worker._target_via_enrichment(mock_post)
                
                assert contact is not None
                # Should target MD for corporate
                call_args = mock_find.call_args
                assert call_args[0][1] == ContactRole.MD

    @pytest.mark.asyncio
    async def test_target_via_enrichment_mid_tier(self, mock_db, mock_post, mock_company, mock_contact):
        """Test targeting via enrichment for mid-tier."""
        worker = TargetingWorker(db=mock_db)
        mock_company.size = "mid_tier"
        
        with patch("app.workers.targeting.repository") as mock_repo:
            mock_repo.get_company.return_value = mock_company
            
            with patch.object(worker.enrichment, "find_contact") as mock_find:
                mock_find.return_value = mock_contact
                
                contact = await worker._target_via_enrichment(mock_post)
                
                assert contact is not None
                # Should target HR for mid-tier
                call_args = mock_find.call_args
                assert call_args[0][1] == ContactRole.HR

    @pytest.mark.asyncio
    async def test_target_via_enrichment_needs_classification(self, mock_db, mock_post, mock_company, mock_contact):
        """Test targeting when company needs classification first."""
        worker = TargetingWorker(db=mock_db)
        mock_company.size = None  # Not yet classified
        
        with patch("app.workers.targeting.repository") as mock_repo:
            mock_repo.get_company.return_value = mock_company
            
            with patch.object(worker.enrichment, "enrich_company") as mock_enrich:
                mock_enrich.return_value = {"company_type": "mid_tier"}
                
                with patch.object(worker.enrichment, "find_contact") as mock_find:
                    mock_find.return_value = mock_contact
                    
                    contact = await worker._target_via_enrichment(mock_post)
                    
                    assert contact is not None
                    mock_enrich.assert_called_once()

    def test_extract_email_from_text(self, mock_db):
        """Test email extraction from text."""
        worker = TargetingWorker(db=mock_db)
        
        text = "Contact us at hiring@techcorp.com for more information"
        email = worker._extract_email_from_text(text)
        
        assert email == "hiring@techcorp.com"

    def test_extract_email_from_text_multiple(self, mock_db):
        """Test email extraction with multiple emails."""
        worker = TargetingWorker(db=mock_db)
        
        text = "Reach out to hr@techcorp.com or careers@techcorp.com"
        email = worker._extract_email_from_text(text)
        
        # Should return first email
        assert email in ["hr@techcorp.com", "careers@techcorp.com"]

    def test_infer_poster_type_founder(self, mock_db, mock_post):
        """Test inferring founder poster type."""
        worker = TargetingWorker(db=mock_db)
        
        mock_post.poster_title = "CEO & Co-Founder"
        poster_type = worker._infer_poster_type(mock_post)
        
        assert poster_type == "founder"

    def test_infer_poster_type_recruiter(self, mock_db, mock_post):
        """Test inferring recruiter poster type."""
        worker = TargetingWorker(db=mock_db)
        
        mock_post.poster_title = "Senior Talent Acquisition Specialist"
        poster_type = worker._infer_poster_type(mock_post)
        
        assert poster_type == "recruiter"

    def test_infer_poster_type_employee(self, mock_db, mock_post):
        """Test inferring employee poster type."""
        worker = TargetingWorker(db=mock_db)
        
        mock_post.poster_title = "Software Engineer"
        poster_type = worker._infer_poster_type(mock_post)
        
        assert poster_type == "employee"

    def test_infer_role_from_title_founder(self, mock_db):
        """Test inferring founder role from title."""
        worker = TargetingWorker(db=mock_db)
        
        role = worker._infer_role_from_title("CEO and Founder")
        assert role == ContactRole.FOUNDER

    def test_infer_role_from_title_hr(self, mock_db):
        """Test inferring HR role from title."""
        worker = TargetingWorker(db=mock_db)
        
        role = worker._infer_role_from_title("HR Manager")
        assert role == ContactRole.HR

    def test_infer_role_from_title_recruiter(self, mock_db):
        """Test inferring recruiter role from title."""
        worker = TargetingWorker(db=mock_db)
        
        role = worker._infer_role_from_title("Senior Recruiter")
        assert role == ContactRole.RECRUITER

    def test_infer_role_from_title_md(self, mock_db):
        """Test inferring MD role from title."""
        worker = TargetingWorker(db=mock_db)
        
        role = worker._infer_role_from_title("Managing Director")
        assert role == ContactRole.MD

    def test_determine_target_role_startup(self, mock_db, mock_company):
        """Test determining target role for startup."""
        worker = TargetingWorker(db=mock_db)
        mock_company.size = "startup"
        
        role = worker._determine_target_role(mock_company)
        
        assert role == ContactRole.FOUNDER

    def test_determine_target_role_corporate(self, mock_db, mock_company):
        """Test determining target role for corporate."""
        worker = TargetingWorker(db=mock_db)
        mock_company.size = "corporate"
        
        role = worker._determine_target_role(mock_company)
        
        assert role == ContactRole.MD

    def test_determine_target_role_mid_tier(self, mock_db, mock_company):
        """Test determining target role for mid-tier."""
        worker = TargetingWorker(db=mock_db)
        mock_company.size = "mid_tier"
        
        role = worker._determine_target_role(mock_company)
        
        assert role == ContactRole.HR
