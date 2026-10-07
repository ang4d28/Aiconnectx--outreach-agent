from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from app.llm.client import LLMClient, LLMError, RateLimitError
from app.llm.prompts import (
    DraftMessageResult,
    PostExtractionResult,
    ReplyClassificationResult,
    classify_reply_prompt,
    draft_message_prompt,
    extract_post_prompt,
)


class TestSchema(BaseModel):
    test_field: str
    test_number: int


class TestLLMClient:
    """Test LLM client functionality."""

    @pytest.fixture
    def llm_client(self):
        return LLMClient()

    @pytest.mark.asyncio
    async def test_groq_success(self, llm_client):
        """Test successful Groq response."""
        mock_response = {
            "choices": [{
                "message": {
                    "content": '{"test_field": "value", "test_number": 42}'
                }
            }]
        }
        
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            mock_response_obj = MagicMock()
            mock_response_obj.status_code = 200
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status.return_value = None
            mock_client.post.return_value = mock_response_obj
            
            with patch("app.llm.client.settings") as mock_settings:
                mock_settings.groq_api_key = "test_key"
                mock_settings.gemini_api_key = ""
                mock_settings.ollama_api_key = ""
                
                result = await llm_client.complete_json("test prompt", TestSchema)
                
                assert result.test_field == "value"
                assert result.test_number == 42

    @pytest.mark.asyncio
    async def test_gemini_fallback(self, llm_client):
        """Test fallback to Gemini when Groq fails."""
        gemini_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"test_field": "gemini_value", "test_number": 24}'
                    }]
                }
            }]
        }
        
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            # Mock Groq failure, then Gemini success
            call_count = 0
            def side_effect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:  # Groq call
                    raise Exception("Groq failed")
                else:  # Gemini call
                    mock_response_obj = MagicMock()
                    mock_response_obj.status_code = 200
                    mock_response_obj.json.return_value = gemini_response
                    mock_response_obj.raise_for_status.return_value = None
                    return mock_response_obj
            
            mock_client.post.side_effect = side_effect
            
            with patch("app.llm.client.settings") as mock_settings:
                mock_settings.groq_api_key = "test_key"
                mock_settings.gemini_api_key = "gemini_key"
                mock_settings.ollama_api_key = ""
                
                result = await llm_client.complete_json("test prompt", TestSchema)
                
                assert result.test_field == "gemini_value"
                assert result.test_number == 24

    @pytest.mark.asyncio
    async def test_ollama_cloud_fallback(self, llm_client):
        """Test fallback to Ollama Cloud when other providers fail."""
        ollama_response = {
            "message": {
                "content": '{"test_field": "ollama_value", "test_number": 12}'
            }
        }
        
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            # Mock first two providers fail, Ollama succeeds
            call_count = 0
            def side_effect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:  # Groq and Gemini fail
                    raise Exception("Provider failed")
                else:  # Ollama succeeds
                    mock_response_obj = MagicMock()
                    mock_response_obj.status_code = 200
                    mock_response_obj.json.return_value = ollama_response
                    mock_response_obj.raise_for_status.return_value = None
                    return mock_response_obj
            
            mock_client.post.side_effect = side_effect
            
            with patch("app.llm.client.settings") as mock_settings:
                mock_settings.groq_api_key = "test_key"
                mock_settings.gemini_api_key = "gemini_key"
                mock_settings.ollama_api_key = "ollama_key"
                mock_settings.ollama_model = "llama3.2"
                
                result = await llm_client.complete_json("test prompt", TestSchema)
                
                assert result.test_field == "ollama_value"
                assert result.test_number == 12

    @pytest.mark.asyncio
    async def test_all_providers_fail(self, llm_client):
        """Test error when all providers fail."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.side_effect = Exception("All providers failed")
            
            with patch("app.llm.client.settings") as mock_settings:
                mock_settings.groq_api_key = "test_key"
                mock_settings.gemini_api_key = "gemini_key"
                mock_settings.ollama_api_key = "ollama_key"
                
                with pytest.raises(LLMError, match="All providers failed"):
                    await llm_client.complete_json("test prompt", TestSchema)

    @pytest.mark.asyncio
    async def test_rate_limit_retry(self, llm_client):
        """Test retry logic for rate limits."""
        success_response = {
            "choices": [{
                "message": {
                    "content": '{"test_field": "retry_success", "test_number": 99}'
                }
            }]
        }
        
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            # Mock rate limit on first call, success on second
            call_count = 0
            def side_effect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                mock_response_obj = MagicMock()
                if call_count == 1:
                    mock_response_obj.status_code = 429
                    mock_response_obj.raise_for_status.side_effect = Exception("Rate limit")
                else:
                    mock_response_obj.status_code = 200
                    mock_response_obj.json.return_value = success_response
                    mock_response_obj.raise_for_status.return_value = None
                return mock_response_obj
            
            mock_client.post.side_effect = side_effect
            
            with patch("app.llm.client.settings") as mock_settings:
                mock_settings.groq_api_key = "test_key"
                mock_settings.gemini_api_key = ""
                mock_settings.ollama_api_key = ""
                
                with patch("asyncio.sleep"):  # Speed up test
                    result = await llm_client.complete_json("test prompt", TestSchema)
                    
                    assert result.test_field == "retry_success"
                    assert result.test_number == 99

    def test_parse_json_with_markdown(self, llm_client):
        """Test parsing JSON with markdown code blocks."""
        content = '```json\n{"test_field": "markdown_value", "test_number": 77}\n```'
        
        result = llm_client._parse_json_response(content, TestSchema)
        
        assert result.test_field == "markdown_value"
        assert result.test_number == 77

    def test_parse_invalid_json(self, llm_client):
        """Test error handling for invalid JSON."""
        content = '{"invalid": json}'
        
        with pytest.raises(LLMError, match="Failed to parse JSON response"):
            llm_client._parse_json_response(content, TestSchema)


class TestPrompts:
    """Test prompt functions and schemas."""

    def test_extract_post_prompt(self):
        """Test post extraction prompt generation."""
        post_text = "We're hiring AI engineers! Apply now."
        
        prompt = extract_post_prompt(post_text)
        
        assert "We're hiring AI engineers! Apply now." in prompt
        assert "hiring/recruitment post" in prompt
        assert "AI/ML B.Tech students" in prompt

    def test_classify_reply_prompt(self):
        """Test reply classification prompt generation."""
        reply_text = "Thanks for reaching out! We're interested."
        
        prompt = classify_reply_prompt(reply_text)
        
        assert "Thanks for reaching out! We're interested." in prompt
        assert "interested:" in prompt
        assert "unsubscribe:" in prompt

    def test_draft_message_prompt(self):
        """Test message drafting prompt generation."""
        target_info = {
            "name": "John Doe",
            "title": "HR Manager",
            "company": "TechCorp"
        }
        context = {
            "post_context": "AI Engineer position",
            "company_size": "100-500"
        }
        
        prompt = draft_message_prompt("linkedin", target_info, context)
        
        assert "John Doe" in prompt
        assert "HR Manager" in prompt
        assert "TechCorp" in prompt
        assert "AI Engineer position" in prompt
        assert "50 to 90 words" in prompt
        assert "AI ConnectX" in prompt

    def test_post_extraction_result_schema(self):
        """Test PostExtractionResult schema validation."""
        valid_data = {
            "is_hiring_post": True,
            "company": "TechCorp",
            "roles": ["AI Engineer", "ML Engineer"],
            "poster_name": "Jane Smith",
            "poster_title": "CTO",
            "poster_type": "founder",
            "contact_email_in_post": "jane@techcorp.com",
            "apply_link": "https://techcorp.com/jobs",
            "relevance_score": 0.9
        }
        
        result = PostExtractionResult(**valid_data)
        
        assert result.is_hiring_post is True
        assert result.company == "TechCorp"
        assert len(result.roles) == 2
        assert result.relevance_score == 0.9

    def test_reply_classification_result_schema(self):
        """Test ReplyClassificationResult schema validation."""
        valid_data = {
            "intent": "interested",
            "referral_name": "Bob Johnson",
            "phone": "+1-555-0123",
            "email": "bob@example.com"
        }
        
        result = ReplyClassificationResult(**valid_data)
        
        assert result.intent == "interested"
        assert result.referral_name == "Bob Johnson"
        assert result.phone == "+1-555-0123"

    def test_draft_message_result_schema(self):
        """Test DraftMessageResult schema validation."""
        valid_data = {
            "message": "Hi John, I saw your AI Engineer post. AI ConnectX connects companies with top B.Tech AI/ML students. Interested in an MoU partnership? Let's chat!",
            "subject": "Partnership Opportunity - AI ConnectX"
        }
        
        result = DraftMessageResult(**valid_data)
        
        assert len(result.message) <= 400
        assert result.subject == "Partnership Opportunity - AI ConnectX"

    def test_draft_message_max_length_validation(self):
        """Test message length validation."""
        invalid_data = {
            "message": "A" * 401,  # Too long
            "subject": "Test"
        }
        
        with pytest.raises(Exception):  # Pydantic validation error
            DraftMessageResult(**invalid_data)