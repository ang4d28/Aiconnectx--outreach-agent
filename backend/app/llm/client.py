from __future__ import annotations

import asyncio
import logging
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)


class LLMError(Exception):
    """Base exception for LLM operations."""
    pass


class RateLimitError(LLMError):
    """Rate limit exceeded."""
    pass


class LLMClient:
    def __init__(self):
        self.timeout = 30.0
        self.max_retries = 3
        self.backoff_factor = 2.0

    async def complete_json(self, prompt: str, schema: type[T]) -> T:
        """
        Complete JSON with validation, trying providers in order.
        
        Tries Groq -> Gemini -> Ollama Cloud on failure/rate limit.
        """
        errors = []
        
        # Try Groq first
        try:
            if settings.groq_api_key:
                result = await self._try_groq(prompt, schema)
                if result:
                    return result
        except Exception as e:
            errors.append(f"Groq: {e}")
            logger.warning(f"Groq failed: {e}")

        # Try Gemini second
        try:
            if settings.gemini_api_key:
                result = await self._try_gemini(prompt, schema)
                if result:
                    return result
        except Exception as e:
            errors.append(f"Gemini: {e}")
            logger.warning(f"Gemini failed: {e}")

        # Try Ollama Cloud as fallback
        try:
            if settings.ollama_api_key:
                result = await self._try_ollama_cloud(prompt, schema)
                if result:
                    return result
        except Exception as e:
            errors.append(f"Ollama: {e}")
            logger.warning(f"Ollama failed: {e}")

        raise LLMError(f"All providers failed: {'; '.join(errors)}")

    async def _try_groq(self, prompt: str, schema: type[T]) -> T | None:
        """Try Groq API with retry and backoff."""
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json"
        }
        
        schema_str = schema.model_json_schema()
        system_prompt = f"""You must respond with valid JSON matching this schema:
{schema_str}

Respond only with the JSON, no additional text."""

        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
            "stream": False
        }

        return await self._make_request_with_retry(url, headers, payload, schema)

    async def _try_gemini(self, prompt: str, schema: type[T]) -> T | None:
        """Try Gemini API with retry and backoff."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={settings.gemini_api_key}"
        headers = {"Content-Type": "application/json"}
        
        schema_str = schema.model_json_schema()
        full_prompt = f"""You must respond with valid JSON matching this schema:
{schema_str}

{prompt}

Respond only with the JSON, no additional text."""

        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2000
            }
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                    
                    if response.status_code == 429:
                        raise RateLimitError("Gemini rate limit exceeded")
                    
                    response.raise_for_status()
                    data = response.json()
                    
                    content = data["candidates"][0]["content"]["parts"][0]["text"]
                    return self._parse_json_response(content, schema)
                    
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    if attempt == self.max_retries - 1:
                        raise LLMError(f"Gemini connection failed: {e}")
                    await asyncio.sleep(self.backoff_factor ** attempt)
                except RateLimitError:
                    if attempt == self.max_retries - 1:
                        raise
                    await asyncio.sleep(self.backoff_factor ** (attempt + 2))

        return None

    async def _try_ollama_cloud(self, prompt: str, schema: type[T]) -> T | None:
        """Try Ollama Cloud API with retry and backoff."""
        url = "https://ollama.com/api/chat"
        headers = {
            "Authorization": f"Bearer {settings.ollama_api_key}",
            "Content-Type": "application/json"
        }
        
        schema_str = schema.model_json_schema()
        system_prompt = f"""You must respond with valid JSON matching this schema:
{schema_str}

Respond only with the JSON, no additional text."""

        payload = {
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 2000
            }
        }

        return await self._make_request_with_retry(url, headers, payload, schema, is_ollama=True)

    async def _make_request_with_retry(
        self, 
        url: str, 
        headers: dict, 
        payload: dict, 
        schema: type[T],
        is_ollama: bool = False
    ) -> T | None:
        """Make HTTP request with retry and backoff logic."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                    
                    if response.status_code == 429:
                        raise RateLimitError("Rate limit exceeded")
                    
                    response.raise_for_status()
                    data = response.json()
                    
                    if is_ollama:
                        content = data["message"]["content"]
                    else:
                        content = data["choices"][0]["message"]["content"]
                    
                    return self._parse_json_response(content, schema)
                    
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    if attempt == self.max_retries - 1:
                        raise LLMError(f"Connection failed: {e}")
                    await asyncio.sleep(self.backoff_factor ** attempt)
                except RateLimitError:
                    if attempt == self.max_retries - 1:
                        raise
                    await asyncio.sleep(self.backoff_factor ** (attempt + 2))

        return None

    def _parse_json_response(self, content: str, schema: type[T]) -> T:
        """Parse and validate JSON response."""
        try:
            # Clean content - remove markdown code blocks if present
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            return schema.model_validate_json(content)
        except (ValidationError, ValueError) as e:
            raise LLMError(f"Failed to parse JSON response: {e}")


# Global instance
llm_client = LLMClient()