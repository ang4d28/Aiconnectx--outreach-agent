from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.redact import redact

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OLLAMA_CHAT_URL = "https://ollama.com/api/chat"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ERROR_BODY_CHARS = 300
MODEL_RETIRED_CACHE_SECONDS = 3600  # a dead model is not tried again for an hour
RETIRED_PHRASES = (
    "no longer available",
    "decommissioned",
    "model_not_found",
    "has been deprecated",
    "is not found for api version",
)
MODEL_USED_PREFIX = "llm_model_used:"  # settings row: the model a provider last answered with
READ_TIMEOUT_ATTEMPTS = 2  # one try and one retry, then the next provider

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)


class LLMError(Exception):
    """Base exception for LLM operations. The message is redacted: it never holds a key or token."""

    def __init__(self, message: object = ""):
        super().__init__(redact(message))


class RateLimitError(LLMError):
    """Rate limit exceeded."""
    pass


class ModelRetiredError(LLMError):
    """The provider says the configured model does not exist (any more)."""


def is_model_retired(status: int, body: str) -> bool:
    return status == 404 or any(phrase in body.lower() for phrase in RETIRED_PHRASES)


def fallback_for(provider: str) -> str:
    value = getattr(settings, f"{provider}_model_fallback", "")
    return value if isinstance(value, str) else ""


def record_model_retired(provider: str, model: str, fallback: str = "") -> None:
    """One "model retired: <provider> <model>" attention item per day. Never raises."""
    try:
        from app.db import SessionLocal
        from app.db.models import AttentionItem, AttentionType, Setting

        today = datetime.now(timezone.utc).date().isoformat()
        key = f"model_retired:{provider}:{model}"
        db = SessionLocal()
        try:
            row = db.get(Setting, key)
            if row is not None and row.value == today:
                return
            if row is None:
                db.add(Setting(key=key, value=today))
            else:
                row.value = today
            db.add(AttentionItem(
                type=AttentionType.NEEDS_MANUAL,
                payload={
                    "reason": f"model retired: {provider} {model}",
                    "provider": provider,
                    "model": model,
                    "fallback": fallback or None,
                },
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"Could not record model retired ({provider} {model}): {redact(exc)}")


def save_model_used(provider: str, model: str) -> None:
    """Remember which model answered, for `doctor`. Never raises."""
    try:
        from app.db import SessionLocal
        from app.db.models import Setting

        db = SessionLocal()
        try:
            row = db.get(Setting, MODEL_USED_PREFIX + provider)
            if row is None:
                db.add(Setting(key=MODEL_USED_PREFIX + provider, value=model))
            else:
                row.value = model
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug(f"Could not save the model used by {provider}: {redact(exc)}")


def gemini_generation_config(model: str, **extra: Any) -> dict[str, Any]:
    """JSON output mode plus the lowest thinking level the model family accepts (a model that does not support
    thinking would reject the field, so older models get none)."""
    config: dict[str, Any] = {"responseMimeType": "application/json", **extra}
    name = model.lower()
    if "gemini-3" in name:
        config["thinkingConfig"] = {"thinkingLevel": "low"}
    elif "gemini-2.5" in name:
        config["thinkingConfig"] = {"thinkingBudget": 128 if "pro" in name else 0}
    return config


def check_response(provider: str, response: Any) -> None:
    """Raise with the status and the first 300 characters of the body on an HTTP error (429 is a RateLimitError)."""
    status = response.status_code
    if status < 400:
        return
    body = redact(getattr(response, "text", "") or "")[:ERROR_BODY_CHARS]
    message = f"{provider} HTTP {status}: {body}"
    if is_model_retired(status, body):
        raise ModelRetiredError(message)
    raise (RateLimitError if status == 429 else LLMError)(message)


class LLMClient:
    def __init__(self):
        self.max_retries = 3
        self.backoff_factor = 2.0
        self.clock = time.monotonic
        self.report_retired = record_model_retired
        self._dead: dict[tuple[str, str], float] = {}
        self._saved_model: dict[str, str] = {}

    def _is_dead(self, provider: str, model: str) -> bool:
        until = self._dead.get((provider, model))
        if until is not None and self.clock() >= until:
            del self._dead[(provider, model)]
            return False
        return until is not None

    async def with_fallback(self, provider: str, call: Any) -> Any:
        """Run call(model) with the configured model; if it is retired, once with the fallback model.
        A retired model is cached as dead for an hour (no request is made for it), logged and recorded."""
        primary = getattr(settings, f"{provider}_model")
        fallback = fallback_for(provider)
        models = [primary] + ([fallback] if fallback and fallback != primary else [])
        retired = []
        detail = "cached as retired, not retried for an hour"
        for model in models:
            if not self._is_dead(provider, model):
                try:
                    result = await call(model)
                except ModelRetiredError as exc:
                    detail = str(exc)
                    self._dead[(provider, model)] = self.clock() + MODEL_RETIRED_CACHE_SECONDS
                    logger.warning(f"model retired: {provider} {model}")
                    self.report_retired(provider, model, fallback if model == primary else "")
                else:
                    if isinstance(model, str) and self._saved_model.get(provider) != model:
                        self._saved_model[provider] = model
                        save_model_used(provider, model)
                    return result
            retired.append(str(model))
        raise ModelRetiredError(f"{provider.capitalize()} model retired ({', '.join(retired)}): {detail}")

    @staticmethod
    def timeout_for(provider: str) -> float:
        return float(getattr(settings, f"{provider}_timeout_seconds"))

    def _read_timeout(self, provider: str, timeouts: int) -> LLMError | None:
        """After READ_TIMEOUT_ATTEMPTS read timeouts return the error that makes the caller fall through."""
        if timeouts < READ_TIMEOUT_ATTEMPTS:
            return None
        seconds = self.timeout_for(provider)
        logger.warning(f"{provider} timeout")
        return LLMError(f"{provider.capitalize()} timeout: no response within {seconds:g}s, {timeouts} attempts")

    async def complete_json(self, prompt: str, schema: type[T]) -> T:
        """
        Complete JSON with validation, trying providers in order.
        
        Tries Groq -> Gemini -> Ollama Cloud on failure/rate limit.
        """
        errors = []
        
        # Try Groq first
        try:
            if settings.groq_api_key:
                result = await self.with_fallback("groq", lambda m: self._try_groq(prompt, schema, m))
                if result:
                    return result
        except Exception as e:
            errors.append(f"Groq: {redact(e)}")
            logger.warning(f"Groq failed: {e}")

        # Try Gemini second
        try:
            if settings.gemini_api_key:
                result = await self.with_fallback("gemini", lambda m: self._try_gemini(prompt, schema, m))
                if result:
                    return result
        except Exception as e:
            errors.append(f"Gemini: {redact(e)}")
            logger.warning(f"Gemini failed: {e}")

        # Try Ollama Cloud as fallback
        try:
            if settings.ollama_api_key:
                result = await self.with_fallback("ollama", lambda m: self._try_ollama_cloud(prompt, schema, m))
                if result:
                    return result
        except Exception as e:
            errors.append(f"Ollama: {redact(e)}")
            logger.warning(f"Ollama failed: {e}")

        raise LLMError(f"All providers failed: {'; '.join(errors)}")

    async def _try_groq(self, prompt: str, schema: type[T], model: str | None = None) -> T | None:
        """Try Groq API with retry and backoff."""
        url = GROQ_URL
        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json"
        }
        
        schema_str = schema.model_json_schema()
        system_prompt = f"""You must respond with valid JSON matching this schema:
{schema_str}

Respond only with the JSON, no additional text."""

        payload = {
            "model": model or settings.groq_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
            "stream": False
        }

        return await self._make_request_with_retry(url, headers, payload, schema)

    async def _try_gemini(self, prompt: str, schema: type[T], model: str | None = None) -> T | None:
        """Try Gemini API with retry and backoff."""
        model = model or settings.gemini_model
        url = GEMINI_URL.format(model=model)
        headers = {"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"}
        
        schema_str = schema.model_json_schema()
        full_prompt = f"""You must respond with valid JSON matching this schema:
{schema_str}

{prompt}

Respond only with the JSON, no additional text."""

        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": gemini_generation_config(model, temperature=0.1, maxOutputTokens=2000),
        }

        timeouts = 0
        async with httpx.AsyncClient(timeout=self.timeout_for("gemini")) as client:
            for attempt in range(self.max_retries):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                    
                    check_response("Gemini", response)
                    data = response.json()
                    
                    content = data["candidates"][0]["content"]["parts"][0]["text"]
                    return self._parse_json_response(content, schema)
                    
                except httpx.ReadTimeout:
                    timeouts += 1
                    if (error := self._read_timeout("gemini", timeouts)) is not None:
                        raise error
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    if attempt == self.max_retries - 1:
                        raise LLMError(f"Gemini connection failed: {e}")
                    await asyncio.sleep(self.backoff_factor ** attempt)
                except RateLimitError:
                    if attempt == self.max_retries - 1:
                        raise
                    await asyncio.sleep(self.backoff_factor ** (attempt + 2))

        return None

    async def _try_ollama_cloud(self, prompt: str, schema: type[T], model: str | None = None) -> T | None:
        """Try Ollama Cloud API with retry and backoff."""
        url = OLLAMA_CHAT_URL
        headers = {
            "Authorization": f"Bearer {settings.ollama_api_key}",
            "Content-Type": "application/json"
        }
        
        schema_str = schema.model_json_schema()
        system_prompt = f"""You must respond with valid JSON matching this schema:
{schema_str}

Respond only with the JSON, no additional text."""

        payload = {
            "model": model or settings.ollama_model,
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
        provider = "ollama" if is_ollama else "groq"
        timeouts = 0
        async with httpx.AsyncClient(timeout=self.timeout_for(provider)) as client:
            for attempt in range(self.max_retries):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                    
                    check_response("Ollama" if is_ollama else "Groq", response)
                    data = response.json()
                    
                    if is_ollama:
                        content = data["message"]["content"]
                    else:
                        content = data["choices"][0]["message"]["content"]
                    
                    return self._parse_json_response(content, schema)
                    
                except httpx.ReadTimeout:
                    timeouts += 1
                    if (error := self._read_timeout(provider, timeouts)) is not None:
                        raise error
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    if attempt == self.max_retries - 1:
                        raise LLMError(f"Connection failed: {e}")
                    await asyncio.sleep(self.backoff_factor ** attempt)
                except RateLimitError:
                    if attempt == self.max_retries - 1:
                        raise
                    await asyncio.sleep(self.backoff_factor ** (attempt + 2))

        return None

    async def ping(self, provider: str) -> str:
        """One tiny request to a provider, no retries: with the configured model, or its fallback when that one
        is retired. Returns the model that answered; raises LLMError ("<Provider> HTTP <status>: <body>")."""
        if provider not in ("groq", "gemini", "ollama"):
            raise LLMError(f"Unknown provider {provider}")
        return await self.with_fallback(provider, lambda model: self._ping_model(provider, model))

    async def _ping_model(self, provider: str, model: str) -> str:
        text = "Reply with the single word OK."
        messages = [{"role": "user", "content": text}]
        if provider == "groq":
            url = GROQ_URL
            headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
            payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": 8}
        elif provider == "gemini":
            url = GEMINI_URL.format(model=model)
            headers = {"x-goog-api-key": settings.gemini_api_key}
            payload = {
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": gemini_generation_config(model, maxOutputTokens=16),
            }
        elif provider == "ollama":
            url = OLLAMA_CHAT_URL
            headers = {"Authorization": f"Bearer {settings.ollama_api_key}"}
            payload = {"model": model, "messages": messages, "stream": False, "options": {"num_predict": 8}}
        else:
            raise LLMError(f"Unknown provider {provider}")
        seconds = self.timeout_for(provider)
        async with httpx.AsyncClient(timeout=seconds) as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException:
                raise LLMError(f"{provider.capitalize()} timeout: no response within {seconds:g}s")
            except httpx.HTTPError as exc:
                raise LLMError(f"{provider} request failed: {type(exc).__name__}: {exc}")
        check_response(provider.capitalize(), response)
        return model

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