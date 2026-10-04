from __future__ import annotations

import asyncio
import logging
import re
import socket
import smtplib
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, repository
from app.db.models import Company, Contact, ContactRole
from app.db.schemas import CompanyUpdate, ContactCreate
from app.llm.client import llm_client
from app.llm.prompts import CompanyClassificationResult, classify_company_prompt

logger = logging.getLogger(__name__)


class EnrichmentWorker:
    def __init__(self, db: Session | None = None):
        self.db = db or SessionLocal()
        self.timeout = 30.0

    async def enrich_company(self, company: Company) -> dict[str, Any]:
        """Enrich company information."""
        logger.info(f"Enriching company: {company.name}")
        
        # Classify company type
        company_type = await self._classify_company(company)
        
        # Update company with classification
        if company_type:
            update_data = CompanyUpdate(size=company_type)
            repository.update_company(self.db, company, update_data)
            logger.info(f"Company {company.name} classified as: {company_type}")
        
        return {
            "company_id": company.id,
            "company_name": company.name,
            "company_type": company_type,
        }

    async def find_contact(
        self, 
        company: Company, 
        role: ContactRole
    ) -> Optional[Contact]:
        """Find contact person by role at company."""
        logger.info(f"Finding {role.value} at {company.name}")
        
        # Search for LinkedIn profile
        linkedin_url = await self._find_linkedin_profile(company.name, role)
        
        if not linkedin_url:
            logger.warning(f"No LinkedIn profile found for {role.value} at {company.name}")
            return None
        
        # Extract name from LinkedIn URL or search results
        name = self._extract_name_from_linkedin_url(linkedin_url)
        
        # Find email
        email, confidence = await self._find_email(company, name)
        
        if confidence < 0.7:
            logger.warning(
                f"Email confidence too low ({confidence}) for {name} at {company.name}"
            )
            return None
        
        # Create contact
        contact_data = ContactCreate(
            company_id=company.id,
            name=name,
            email=email,
            linkedin_url=linkedin_url,
            title=self._role_to_title(role),
            role=role,
        )
        
        contact = repository.create_contact(self.db, contact_data)
        logger.info(f"Created contact: {name} ({email}) with confidence {confidence}")
        
        return contact

    async def _classify_company(self, company: Company) -> Optional[str]:
        """Classify company type using LLM."""
        company_info = f"""
        Name: {company.name}
        Website: {company.website or 'Unknown'}
        LinkedIn: {company.linkedin_url or 'Unknown'}
        Industry: {company.industry or 'Unknown'}
        """
        
        try:
            prompt = classify_company_prompt(company.name, company_info)
            result: CompanyClassificationResult = await llm_client.complete_json(
                prompt, CompanyClassificationResult
            )
            
            return result.company_type
            
        except Exception as e:
            logger.error(f"Failed to classify company {company.name}: {e}")
            return None

    async def _find_linkedin_profile(
        self, 
        company_name: str, 
        role: ContactRole
    ) -> Optional[str]:
        """Find LinkedIn profile URL for role at company."""
        role_keywords = self._role_to_search_keywords(role)
        query = f'site:linkedin.com/in "{role_keywords}" "{company_name}"'
        
        try:
            results = await self._search_linkedin_duckduckgo(query)
            
            # Return first LinkedIn profile URL
            for result in results:
                url = result.get("url", "")
                if "linkedin.com/in/" in url:
                    return url
            
            return None
            
        except Exception as e:
            logger.error(f"LinkedIn search failed: {e}")
            return None

    async def _search_linkedin_duckduckgo(self, query: str) -> list[dict[str, Any]]:
        """Search LinkedIn profiles via DuckDuckGo."""
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return []
        
        results = []
        try:
            with DDGS() as ddgs:
                search_results = ddgs.text(query, max_results=5)
                for item in search_results:
                    results.append({
                        "url": item.get("href", ""),
                        "title": item.get("title", ""),
                    })
        except Exception as e:
            logger.error(f"DuckDuckGo search failed: {e}")
        
        return results

    async def _find_email(
        self, 
        company: Company, 
        name: str
    ) -> tuple[Optional[str], float]:
        """Find and verify email address."""
        if not company.website:
            return None, 0.0
        
        # Try scraping company website
        scraped_emails = await self._scrape_company_emails(company.website)
        
        # Try generating email patterns
        generated_emails = self._generate_email_patterns(name, company.website)
        
        # Combine and verify
        candidate_emails = list(set(scraped_emails + generated_emails))
        
        for email in candidate_emails:
            confidence = await self._verify_email(email)
            if confidence >= 0.7:
                return email, confidence
        
        return None, 0.0

    async def _scrape_company_emails(self, website: str) -> list[str]:
        """Scrape emails from company website."""
        if not website.startswith("http"):
            website = f"https://{website}"
        
        emails = []
        pages_to_check = [
            website,
            f"{website}/contact",
            f"{website}/careers",
            f"{website}/about",
        ]
        
        for page_url in pages_to_check:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(page_url, follow_redirects=True)
                    if response.status_code != 200:
                        continue
                    
                    html = HTMLParser(response.text)
                    text = html.body.text() if html.body else response.text
                    
                    # Extract emails using regex
                    found_emails = re.findall(
                        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
                        text
                    )
                    
                    # Filter out common non-personal emails
                    filtered = [
                        e for e in found_emails
                        if not any(
                            skip in e.lower()
                            for skip in ["noreply", "info@", "support@", "hello@"]
                        )
                    ]
                    
                    emails.extend(filtered)
                    
            except Exception as e:
                logger.debug(f"Failed to scrape {page_url}: {e}")
                continue
        
        return list(set(emails))

    def _generate_email_patterns(self, name: str, website: str) -> list[str]:
        """Generate common email patterns."""
        if not name or not website:
            return []
        
        # Extract domain from website
        parsed = urlparse(website if "://" in website else f"https://{website}")
        domain = parsed.netloc or website
        domain = domain.replace("www.", "")
        
        # Parse name
        name_parts = name.lower().split()
        if len(name_parts) < 2:
            return []
        
        first = name_parts[0]
        last = name_parts[-1]
        
        # Common patterns
        patterns = [
            f"{first}@{domain}",
            f"{first}.{last}@{domain}",
            f"{first[0]}{last}@{domain}",
            f"{first}_{last}@{domain}",
            f"{last}@{domain}",
        ]
        
        return patterns

    async def _verify_email(self, email: str) -> float:
        """Verify email address using DNS MX and SMTP RCPT check."""
        confidence = 0.0
        
        # Extract domain
        try:
            domain = email.split("@")[1]
        except IndexError:
            return 0.0
        
        # Check MX records
        try:
            mx_records = socket.getaddrinfo(domain, 25, socket.AF_INET, socket.SOCK_STREAM)
            if mx_records:
                confidence = 0.5
        except Exception:
            return 0.0
        
        # SMTP RCPT check
        try:
            with smtplib.SMTP(timeout=10) as smtp:
                smtp.connect(domain, 25)
                smtp.helo("aiconnectx.com")
                smtp.mail("verify@aiconnectx.com")
                code, _ = smtp.rcpt(email)
                
                if code == 250:
                    confidence = 0.9
                elif code == 251:
                    confidence = 0.7
                    
        except Exception as e:
            logger.debug(f"SMTP verification failed for {email}: {e}")
        
        return confidence

    def _extract_name_from_linkedin_url(self, linkedin_url: str) -> str:
        """Extract name from LinkedIn URL."""
        # Try to extract from URL slug (e.g., /in/john-doe-123/)
        match = re.search(r'/in/([^/]+)', linkedin_url)
        if match:
            slug = match.group(1)
            # Remove numbers and convert dashes to spaces
            name = re.sub(r'-?\d+$', '', slug)
            name = name.replace('-', ' ')
            return name.title()
        
        return "Unknown"

    def _role_to_search_keywords(self, role: ContactRole) -> str:
        """Convert ContactRole to search keywords."""
        mapping = {
            ContactRole.HR: "HR Manager OR Human Resources OR Talent Acquisition",
            ContactRole.FOUNDER: "Founder OR CEO OR Co-Founder",
            ContactRole.MD: "Managing Director OR MD OR Director",
            ContactRole.RECRUITER: "Recruiter OR Hiring Manager",
            ContactRole.OTHER: "Contact",
        }
        return mapping.get(role, "Contact")

    def _role_to_title(self, role: ContactRole) -> str:
        """Convert ContactRole to job title."""
        mapping = {
            ContactRole.HR: "HR Manager",
            ContactRole.FOUNDER: "Founder",
            ContactRole.MD: "Managing Director",
            ContactRole.RECRUITER: "Recruiter",
            ContactRole.OTHER: "Contact",
        }
        return mapping.get(role, "Contact")
