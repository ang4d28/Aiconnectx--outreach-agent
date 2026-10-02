from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.db import SessionLocal, repository
from app.db.models import Company, Contact, ContactRole, Post
from app.db.schemas import ContactCreate
from app.llm.prompts import PostExtractionResult
from app.workers.enrichment import EnrichmentWorker

logger = logging.getLogger(__name__)


class TargetingWorker:
    def __init__(self, db: Session | None = None):
        self.db = db or SessionLocal()
        self.enrichment = EnrichmentWorker(db=self.db)

    async def pick_target(self, post: Post) -> Optional[Contact]:
        """
        Pick the right contact for a post.
        
        Rules:
        1. Email in post -> use that email directly
        2. Poster is recruiter/founder -> target the poster
        3. Poster is employee/company_page -> find HR (mid-tier), founder (startup), or MD (corporate)
        """
        logger.info(f"Picking target for post {post.id}")
        
        # Extract post metadata (should already be in Post model)
        # If email in post, create direct contact
        if post.poster_profile_url and "@" in (post.raw_text or ""):
            email = self._extract_email_from_text(post.raw_text or "")
            if email:
                return await self._create_direct_contact(post, email)
        
        # Determine poster type from post
        poster_type = self._infer_poster_type(post)
        
        # If poster is recruiter or founder, target the poster
        if poster_type in ["recruiter", "founder"]:
            return await self._target_poster(post)
        
        # Otherwise, need to find appropriate contact via enrichment
        return await self._target_via_enrichment(post)

    async def _create_direct_contact(self, post: Post, email: str) -> Optional[Contact]:
        """Create contact with email from post."""
        if not post.company_id:
            logger.warning(f"Post {post.id} has no company_id")
            return None
        
        # Check if contact already exists
        existing = repository.get_contact_by_email(self.db, email)
        if existing:
            logger.info(f"Contact with email {email} already exists")
            return existing
        
        contact_data = ContactCreate(
            company_id=post.company_id,
            name=post.poster_name or "Unknown",
            email=email,
            linkedin_url=post.poster_profile_url,
            title=post.poster_title,
            role=ContactRole.OTHER,
        )
        
        contact = repository.create_contact(self.db, contact_data)
        logger.info(f"Created direct contact from post: {email}")
        return contact

    async def _target_poster(self, post: Post) -> Optional[Contact]:
        """Target the poster directly."""
        if not post.company_id:
            logger.warning(f"Post {post.id} has no company_id")
            return None
        
        if not post.poster_name:
            logger.warning(f"Post {post.id} has no poster name")
            return None
        
        # Check if contact already exists
        if post.poster_profile_url:
            existing_contacts = repository.get_contacts_by_company(
                self.db, post.company_id
            )
            for contact in existing_contacts:
                if contact.linkedin_url == post.poster_profile_url:
                    logger.info(f"Poster contact already exists: {contact.name}")
                    return contact
        
        # Determine role based on title
        role = self._infer_role_from_title(post.poster_title or "")
        
        contact_data = ContactCreate(
            company_id=post.company_id,
            name=post.poster_name,
            email=None,  # Will be enriched later
            linkedin_url=post.poster_profile_url,
            title=post.poster_title,
            role=role,
        )
        
        contact = repository.create_contact(self.db, contact_data)
        logger.info(f"Created poster contact: {post.poster_name}")
        
        # Try to find email
        if not contact.email:
            company = repository.get_company(self.db, post.company_id)
            if company:
                email, confidence = await self.enrichment._find_email(
                    company, contact.name
                )
                if email and confidence >= 0.7:
                    from app.db.schemas import ContactUpdate
                    update_data = ContactUpdate(email=email)
                    repository.update_contact(self.db, contact, update_data)
                    logger.info(f"Added email to poster contact: {email}")
        
        return contact

    async def _target_via_enrichment(self, post: Post) -> Optional[Contact]:
        """Find target contact via enrichment based on company type."""
        if not post.company_id:
            logger.warning(f"Post {post.id} has no company_id")
            return None
        
        company = repository.get_company(self.db, post.company_id)
        if not company:
            logger.warning(f"Company {post.company_id} not found")
            return None
        
        # Enrich company if not already classified
        if not company.size:
            await self.enrichment.enrich_company(company)
            # Refresh company object
            company = repository.get_company(self.db, post.company_id)
        
        # Determine target role based on company type
        target_role = self._determine_target_role(company)
        
        # Find contact
        contact = await self.enrichment.find_contact(company, target_role)
        
        if not contact:
            logger.warning(
                f"Could not find {target_role.value} contact for {company.name}"
            )
        
        return contact

    def _extract_email_from_text(self, text: str) -> Optional[str]:
        """Extract email from text."""
        import re
        matches = re.findall(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            text
        )
        return matches[0] if matches else None

    def _infer_poster_type(self, post: Post) -> str:
        """Infer poster type from post metadata."""
        if not post.poster_title:
            return "employee"
        
        title_lower = post.poster_title.lower()
        
        if any(keyword in title_lower for keyword in ["founder", "ceo", "co-founder"]):
            return "founder"
        
        if any(keyword in title_lower for keyword in ["recruiter", "talent", "hr"]):
            return "recruiter"
        
        if "company page" in (post.poster_name or "").lower():
            return "company_page"
        
        return "employee"

    def _infer_role_from_title(self, title: str) -> ContactRole:
        """Infer ContactRole from job title."""
        title_lower = title.lower()
        
        if any(keyword in title_lower for keyword in ["founder", "ceo", "co-founder"]):
            return ContactRole.FOUNDER
        
        if any(keyword in title_lower for keyword in ["recruiter", "talent acquisition"]):
            return ContactRole.RECRUITER
        
        if any(keyword in title_lower for keyword in ["managing director", "md", "director"]):
            return ContactRole.MD
        
        if "hr" in title_lower or "human resources" in title_lower:
            return ContactRole.HR
        
        return ContactRole.OTHER

    def _determine_target_role(self, company: Company) -> ContactRole:
        """Determine target role based on company classification."""
        company_type = (company.size or "").lower()
        
        if "startup" in company_type:
            return ContactRole.FOUNDER
        
        if "corporate" in company_type:
            return ContactRole.MD
        
        # Default to HR for mid-tier or unknown
        return ContactRole.HR
