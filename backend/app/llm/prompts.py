from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class PostExtractionResult(BaseModel):
    """Result of extracting information from a LinkedIn post."""
    is_hiring_post: bool = Field(..., description="Whether this is a hiring/job post")
    company: Optional[str] = Field(None, description="Company name from the post")
    roles: list[str] = Field(default_factory=list, description="Job roles mentioned")
    poster_name: Optional[str] = Field(None, description="Name of the person who posted")
    poster_title: Optional[str] = Field(None, description="Job title of the poster")
    poster_type: Literal["recruiter", "founder", "employee", "company_page"] = Field(
        ..., description="Type of poster based on their role"
    )
    contact_email_in_post: Optional[str] = Field(None, description="Email mentioned in post")
    apply_link: Optional[str] = Field(None, description="Application link if present")
    relevance_score: float = Field(..., ge=0, le=1, description="Relevance to AI/ML B.Tech students (0-1)")


class ReplyClassificationResult(BaseModel):
    """Result of classifying a reply message."""
    intent: Literal[
        "interested", 
        "no_opportunity", 
        "wrong_person", 
        "not_now", 
        "question", 
        "unsubscribe", 
        "other"
    ] = Field(..., description="Primary intent of the reply")
    referral_name: Optional[str] = Field(None, description="Name if they refer someone else")
    phone: Optional[str] = Field(None, description="Phone number if provided")
    email: Optional[str] = Field(None, description="Email address if provided")


class DraftMessageResult(BaseModel):
    """Result of drafting a message."""
    message: str = Field(..., max_length=400, description="Drafted message under 80 words")
    subject: Optional[str] = Field(None, description="Email subject line if applicable")


class CompanyClassificationResult(BaseModel):
    """Result of classifying a company."""
    company_type: Literal["startup", "mid_tier", "corporate"] = Field(
        ..., description="Company type classification"
    )
    employee_count_estimate: Optional[int] = Field(None, description="Estimated employee count")
    reasoning: str = Field(..., description="Brief reasoning for classification")


def extract_post_prompt(post_text: str) -> str:
    """Prompt for extracting information from a LinkedIn post."""
    return f"""Analyze this LinkedIn post and extract key information for university placement outreach:

POST TEXT:
{post_text}

Determine:
1. Is this a hiring/recruitment post?
2. What company is posting?
3. What job roles are mentioned?
4. Who is the poster and what's their role?
5. Classify poster as: recruiter, founder, employee, or company_page
6. Any contact email mentioned?
7. Application links present?
8. How relevant is this for AI/ML B.Tech students? (0.0 = not relevant, 1.0 = highly relevant)

Focus on posts seeking fresh graduates, entry-level positions, or internships in AI/ML, software development, or tech roles."""


def classify_reply_prompt(reply_text: str) -> str:
    """Prompt for classifying a reply message."""
    return f"""Classify this reply message from a LinkedIn/email outreach:

REPLY TEXT:
{reply_text}

Determine the primary intent:
- interested: They want to engage/learn more
- no_opportunity: No current openings
- wrong_person: They're not the right contact
- not_now: Maybe later/future opportunities
- question: They're asking for clarification
- unsubscribe: They want to stop receiving messages
- other: Doesn't fit above categories

Also extract any:
- Names if they refer someone else
- Phone numbers provided
- Email addresses provided"""


def draft_message_prompt(
    channel: str,
    target_info: dict,
    context: dict,
    sender: dict | None = None,
    facts: str = "",
    retry_reason: str = "",
    instruction: str = "",
) -> str:
    """Prompt for drafting an outreach message. The code adds the greeting and the signature.

    instruction is an optional style request from the owner ("make it shorter"); it never overrides the rules."""
    sender = sender or {}
    org = sender.get("org", "AI ConnectX")
    university = sender.get("university", "Universal AI University")
    has_post = context.get("has_post", "yes" if context.get("post_context") else "no") == "yes"
    post_rule = (
        f"- Refer to this hiring post or role once, naturally: {context.get('post_context', '')}"
        if has_post
        else "- There is no hiring post for this lead: do not mention any post, role or vacancy"
    )
    facts_block = facts.strip() or "(none: make no claims about the organisation beyond its name and the ask)"
    retry = (
        f"\nYOUR PREVIOUS DRAFT WAS REJECTED: {retry_reason}. Write a new draft that fixes this.\n"
        if retry_reason
        else ""
    )
    owner_request = (
        f"\nOWNER'S STYLE REQUEST (tone and length only; every rule below still applies): {instruction.strip()}\n"
        if instruction.strip()
        else ""
    )
    return f"""Write a short {channel} outreach message.

SENDER (the person writing; write in the first person, "I" or "we"):
- The {org} team, the placement outreach initiative of {university}
- The sender is NOT the recipient and has no connection to the recipient's company.

RECIPIENT (the person receiving the message; address them as "you"):
- Name: {target_info.get('name', 'N/A')}
- Title: {target_info.get('title', 'N/A')}
- Company: {target_info.get('company', 'N/A')}
- Company size: {context.get('company_size', 'Unknown')}

FACTS (the only claims you may make about {org} and {university}):
{facts_block}
{retry}{owner_request}
RULES:
- Use only facts from the FACTS section. No invented statistics, numbers, rankings, "premier" or "leading", no promises or guarantees.
- Mention {university} by name once.
- Never say or imply that the sender holds the recipient's title or works at the recipient's company; the recipient does.
{post_rule}
- 50 to 90 words in the body, one clear ask (a short call, a meeting or a reply), professional but warm.
- Do not write a greeting or a name line (no "Hi ...", no "Dear ...") and no signature or closing: the system adds both.
- No placeholders of any kind: no square brackets, no curly braces, no "Your Name".
- No generic template language, nothing salesy, one ask only.

Return JSON with "subject" (at most 8 words, no brackets) and "message" (the body only)."""


def classify_company_prompt(company_name: str, company_info: str) -> str:
    """Prompt for classifying company type."""
    return f"""Classify this company into one of three categories: startup, mid_tier, or corporate.

COMPANY: {company_name}

COMPANY INFO:
{company_info}

Classification criteria:
- startup: <50 employees, early stage, founder-led, VC-backed or bootstrapped
- mid_tier: 50-500 employees, established but growing, professional HR
- corporate: >500 employees, large enterprise, structured recruiting

Provide:
1. Classification (startup, mid_tier, or corporate)
2. Estimated employee count if determinable
3. Brief reasoning for your classification"""
