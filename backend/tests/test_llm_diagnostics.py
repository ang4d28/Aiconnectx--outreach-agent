"""Secret redaction, LLM error messages, the env override warning, `test-llm` and `doctor`."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pydantic
import pytest
from sqlalchemy import text

from app import cli, config, doctor, redact, scheduler
from app.channel_config import unregistered_jobs
from app.config import env_overrides, settings
from app.llm.client import LLMClient, LLMError, RateLimitError, gemini_generation_config
from tests.conftest import test_engine

SECRET = "AIzaSyDUMMYKEY0123456789abcdefghij"


def mock_http(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.llm.client.httpx.AsyncClient", factory)


class TestRedact:
    def test_masks_bearer_key_param_and_known_shapes(self):
        out = redact.redact(f"Bearer abc.def-123 https://x/y?key={SECRET}&a=1 token=zzz {SECRET} x-goog-api-key: {SECRET}")
        assert "abc.def" not in out and SECRET not in out and "zzz" not in out
        assert "key=[REDACTED]&a=1" in out

    def test_masks_the_exact_secrets_in_settings(self):
        assert settings.groq_api_key not in redact.redact(f"failed with {settings.groq_api_key}")

    def test_every_log_record_is_redacted_including_httpx(self, caplog):
        redact.install()
        redact.install()  # idempotent
        with caplog.at_level(logging.INFO):
            logging.getLogger("httpx").info('HTTP Request: POST https://g.example/m?key=%s "200 OK"', SECRET)
            try:
                raise RuntimeError(f"boom Bearer {SECRET}")
            except RuntimeError:
                logging.getLogger("app.x").exception("failed")
        assert SECRET not in caplog.text
        assert "key=[REDACTED]" in caplog.text


class TestLLMErrors:
    @pytest.mark.asyncio
    async def test_gemini_key_is_a_header_not_in_the_url(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"], seen["headers"] = str(request.url), request.headers
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})

        mock_http(monkeypatch, handler)
        assert await LLMClient().ping("gemini") == settings.gemini_model
        assert "key" not in seen["url"].lower() and settings.gemini_api_key not in seen["url"]
        assert seen["url"].endswith(f"/{settings.gemini_model}:generateContent")
        assert seen["headers"]["x-goog-api-key"] == settings.gemini_api_key

    @pytest.mark.asyncio
    async def test_http_error_has_status_and_first_300_chars_of_body(self, monkeypatch):
        body = "E" * 500
        mock_http(monkeypatch, lambda request: httpx.Response(404, text=body))
        with pytest.raises(LLMError) as err:
            await LLMClient().ping("groq")
        assert str(err.value).endswith(f"Groq HTTP 404: {'E' * 300}")  # status and body survive

    @pytest.mark.asyncio
    async def test_error_message_never_holds_a_key(self, monkeypatch):
        body = f'{{"error": "API key not valid: {settings.gemini_api_key}", "url": "?key={SECRET}"}}'
        mock_http(monkeypatch, lambda request: httpx.Response(400, text=body))
        with pytest.raises(LLMError) as err:
            await LLMClient().ping("gemini")
        assert "HTTP 400" in str(err.value)
        assert settings.gemini_api_key not in str(err.value) and SECRET not in str(err.value)

    @pytest.mark.asyncio
    async def test_429_is_a_rate_limit_error_with_the_body(self, monkeypatch):
        mock_http(monkeypatch, lambda request: httpx.Response(429, text="slow down"))
        with pytest.raises(RateLimitError, match="Groq HTTP 429: slow down"):
            await LLMClient().ping("groq")

    def test_configured_models_are_used(self):
        assert settings.groq_model == "test-groq-model"  # no hard-coded model name any more
        assert LLMClient().ping.__qualname__  # ping reads the model from settings (see the URL test above)


class TestEnvOverrides:
    def test_warns_by_name_only_when_values_differ(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("GEMINI_MODEL=gemini-a\nGROQ_MODEL=same\nOLLAMA_MODEL=o\n", encoding="utf-8")
        found = env_overrides(str(env), {"GEMINI_MODEL": "gemini-b", "GROQ_MODEL": "same"})
        assert found == ["GEMINI_MODEL"]

    def test_no_env_file_or_no_difference_is_quiet(self, tmp_path):
        assert env_overrides(None, {"GEMINI_MODEL": "x"}) == []
        assert env_overrides(str(tmp_path / "missing"), {"GEMINI_MODEL": "x"}) == []

    def test_warning_names_the_variable_not_the_value(self, monkeypatch, caplog):
        from app import config

        monkeypatch.setattr(config, "env_overrides", lambda: ["GROQ_MODEL"])
        with caplog.at_level(logging.WARNING):
            config.warn_env_overrides()
        assert "GROQ_MODEL" in caplog.text


class TestTestLLM:
    def test_ok_and_fail_per_provider_without_keys_in_output(self, monkeypatch, capsys):
        async def ping(self, provider):
            if provider == "gemini":
                raise LLMError(f"Gemini HTTP 403: key {settings.gemini_api_key} rejected")
            return settings.groq_model

        monkeypatch.setattr(LLMClient, "ping", ping)
        monkeypatch.setattr(settings, "ollama_api_key", "")
        assert cli.main(["test-llm"]) == 1
        captured = capsys.readouterr()
        assert f"OK groq ({settings.groq_model})" in captured.out
        assert "SKIPPED ollama" in captured.out
        assert "FAIL gemini" in captured.err and "HTTP 403" in captured.err
        for key in (settings.groq_api_key, settings.gemini_api_key):
            assert key not in captured.out + captured.err

    def test_no_keys_is_a_failure(self, monkeypatch, capsys):
        for name in ("groq_api_key", "gemini_api_key", "ollama_api_key"):
            monkeypatch.setattr(settings, name, "")
        assert cli.main(["test-llm"]) == 1
        assert "No provider has an API key" in capsys.readouterr().err


def rows_by_name():
    return {name: (status, detail) for name, status, detail in doctor.collect()}


@pytest.fixture
def at_head(db):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from app import db as app_db

    head = ScriptDirectory.from_config(Config(str(Path(app_db.__file__).parent / "alembic.ini"))).get_current_head()
    with test_engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    yield head
    with test_engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))


class TestDoctor:
    def test_everything_configured(self, at_head):
        rows = rows_by_name()
        assert rows["Database"][0] == "OK"
        assert rows["Migrations"] == ("OK", f"at head ({at_head})")
        assert [rows[name][0] for name in ("Outlook", "Whatsapp", "LinkedIn")] == ["OK"] * 3
        assert rows["DRY_RUN"][0] == "OK" and "nothing is sent" in rows["DRY_RUN"][1]
        assert rows["SEND_ALLOWLIST"][0] == "SKIPPED"
        assert rows["LLM Groq"][0] == "OK"
        assert doctor.run_doctor(lambda line: None) == 0

    def test_migrations_behind_is_a_failure(self, db):
        status, detail = rows_by_name()["Migrations"]
        assert status == "FAIL" and "upgrade head" in detail

    def test_missing_settings_skip_the_channel_and_name_them(self, at_head, monkeypatch):
        monkeypatch.setattr(settings, "whatsapp_token", "")
        monkeypatch.setattr(settings, "whatsapp_owner_number", "")
        monkeypatch.setattr(settings, "ms_client_id", "")
        rows = rows_by_name()
        assert rows["Whatsapp"] == ("SKIPPED", "missing WHATSAPP_TOKEN, WHATSAPP_OWNER_NUMBER")
        assert rows["Outlook"] == ("SKIPPED", "missing MS_CLIENT_ID")
        assert "daily_summary (whatsapp settings missing)" in rows["Scheduler jobs"][1]
        assert doctor.run_doctor(lambda line: None) == 0  # skipped is never an error

    def test_linkedin_disabled_and_no_llm_keys(self, at_head, monkeypatch):
        monkeypatch.setattr(settings, "linkedin_enabled", False)
        for name in ("groq_api_key", "gemini_api_key", "ollama_api_key"):
            monkeypatch.setattr(settings, name, "")
        rows = rows_by_name()
        assert rows["LinkedIn"] == ("SKIPPED", "disabled (LINKEDIN_ENABLED=false)")
        assert rows["LLM"][0] == "FAIL"

    def test_allowlist_and_unreachable_database(self, monkeypatch):
        monkeypatch.setattr(settings, "send_allowlist", "me@test.com")
        monkeypatch.setattr(doctor, "check_database", lambda: ("Database", "FAIL", "OperationalError: down"))
        rows = rows_by_name()
        assert rows["SEND_ALLOWLIST"] == ("OK", "active (1 address(es))")
        assert rows["Migrations"][0] == "SKIPPED" and rows["Job store"][0] == "SKIPPED"

    def test_cli_prints_a_table(self, at_head, capsys):
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "Database" in out and "SKIPPED" in out and "No failures" in out
        assert settings.groq_api_key not in out


class TestUnregisteredJobs:
    def test_missing_settings_drop_only_that_channels_jobs(self, monkeypatch):
        ids = {spec.id for spec in scheduler.build_specs()}
        assert {"linkedin_session", "connection_poll", "daily_summary", "send_queue"} <= ids
        monkeypatch.setattr(settings, "linkedin_profile_dir", "")
        monkeypatch.setattr(settings, "whatsapp_token", "")
        monkeypatch.setattr(settings, "ms_client_id", "")  # Outlook has no job of its own
        left = {spec.id for spec in scheduler.build_specs()}
        assert ids - left == {"linkedin_session", "connection_poll", "daily_summary"}
        assert unregistered_jobs()["daily_summary"] == "whatsapp"


class TimeoutSchema(pydantic.BaseModel):
    answer: str


class TestTimeouts:
    def test_per_provider_defaults(self):
        fields = config.Settings.model_fields
        assert (fields["gemini_timeout_seconds"].default, fields["groq_timeout_seconds"].default,
                fields["ollama_timeout_seconds"].default) == (60, 30, 60)

    @pytest.mark.asyncio
    async def test_each_provider_uses_its_own_timeout(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_timeout_seconds", 61)
        monkeypatch.setattr(settings, "groq_timeout_seconds", 31)
        monkeypatch.setattr(settings, "ollama_timeout_seconds", 62)
        seen = {}
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            def handler(request):
                seen[request.url.host] = kwargs["timeout"]
                return httpx.Response(200, json={})

            return real(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr("app.llm.client.httpx.AsyncClient", factory)
        for provider in ("groq", "gemini", "ollama"):
            await LLMClient().ping(provider)
        assert seen == {"api.groq.com": 31, "generativelanguage.googleapis.com": 61, "ollama.com": 62}

    @pytest.mark.asyncio
    async def test_read_timeout_retries_once_then_falls_through_to_the_next_provider(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "groq_api_key", "")
        calls = []

        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "ollama.com":
                return httpx.Response(200, json={"message": {"content": '{"answer": "from ollama"}'}})
            raise httpx.ReadTimeout("read timed out", request=request)

        mock_http(monkeypatch, handler)
        with caplog.at_level(logging.WARNING):
            result = await LLMClient().complete_json("q", TimeoutSchema)
        assert result.answer == "from ollama"
        assert calls == ["generativelanguage.googleapis.com"] * 2 + ["ollama.com"]  # one retry, then the next one
        assert "gemini timeout" in caplog.text
        assert settings.gemini_api_key not in caplog.text

    @pytest.mark.asyncio
    async def test_ping_reports_a_timeout_without_the_key(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("read timed out", request=request)

        mock_http(monkeypatch, handler)
        with pytest.raises(LLMError, match=r"Gemini timeout: no response within 60s") as err:
            await LLMClient().ping("gemini")
        assert settings.gemini_api_key not in str(err.value)

    def test_test_llm_reports_elapsed_time(self, monkeypatch, capsys):
        async def ping(self, provider):
            if provider == "gemini":
                raise LLMError("Gemini timeout: no response within 60s")
            return "m"

        monkeypatch.setattr(LLMClient, "ping", ping)
        assert cli.main(["test-llm"]) == 1
        captured = capsys.readouterr()
        assert re.search(r"OK groq \(.*\) \d+\.\ds", captured.out)
        assert re.search(r"FAIL gemini \(.*\) after \d+\.\ds: Gemini timeout", captured.err)


class TestGeminiRequest:
    @pytest.mark.parametrize(
        "model, thinking",
        [
            ("gemini-2.5-flash", {"thinkingBudget": 0}),
            ("gemini-2.5-pro", {"thinkingBudget": 128}),
            ("gemini-3-flash-preview", {"thinkingLevel": "low"}),
            ("gemini-2.0-flash-exp", None),  # no thinking support: the field would be rejected
        ],
    )
    def test_json_mode_and_lowest_thinking_level_per_model_family(self, model, thinking):
        generation = gemini_generation_config(model, maxOutputTokens=16)
        assert generation["responseMimeType"] == "application/json" and generation["maxOutputTokens"] == 16
        assert generation.get("thinkingConfig") == thinking

    @pytest.mark.asyncio
    async def test_normal_calls_and_ping_send_json_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "")
        monkeypatch.setattr(settings, "gemini_model", "gemini-2.5-flash")
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"answer": "x"}'}]}}]})

        mock_http(monkeypatch, handler)
        await LLMClient().complete_json("q", TimeoutSchema)
        await LLMClient().ping("gemini")
        for body in bodies:
            assert body["generationConfig"]["responseMimeType"] == "application/json"
            assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


GROQ_OK = {"choices": [{"message": {"content": '{"answer": "ok"}'}}]}
GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": '{"answer": "gemini"}'}]}}]}
RETIRED_404 = '{"error": {"message": "model not found", "code": "model_not_found"}}'


class TestModelRetired:
    @pytest.fixture
    def groq_only(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "")
        monkeypatch.setattr(settings, "ollama_api_key", "")
        monkeypatch.setattr(settings, "groq_model_fallback", "groq-fallback")

    def groq_handler(self, seen, dead=("test-groq-model",), status=404, body=RETIRED_404):
        def handler(request):
            if request.url.host != "api.groq.com":
                return httpx.Response(200, json=GEMINI_OK)
            model = json.loads(request.content)["model"]
            seen.append(model)
            return httpx.Response(status, text=body) if model in dead else httpx.Response(200, json=GROQ_OK)

        return handler

    @pytest.mark.asyncio
    async def test_404_tries_the_fallback_once_and_never_the_dead_model_again(self, monkeypatch, groq_only, caplog):
        seen, now = [], [0.0]
        client = LLMClient()
        client.clock = lambda: now[0]
        reports = []
        client.report_retired = lambda *args: reports.append(args)
        mock_http(monkeypatch, self.groq_handler(seen))
        with caplog.at_level(logging.WARNING):
            assert (await client.complete_json("q", TimeoutSchema)).answer == "ok"
            assert (await client.complete_json("q", TimeoutSchema)).answer == "ok"
        assert seen == ["test-groq-model", "groq-fallback", "groq-fallback"]  # dead model asked once
        assert reports == [("groq", "test-groq-model", "groq-fallback")]
        assert "model retired: groq test-groq-model" in caplog.text and settings.groq_api_key not in caplog.text
        now[0] = 3601  # the failure cache expires after an hour
        await client.complete_json("q", TimeoutSchema)
        assert seen[-2:] == ["test-groq-model", "groq-fallback"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status, body", [(400, '{"error": "The model has been decommissioned"}'),
                                              (400, "This model is no longer available")])
    async def test_no_longer_available_wording_counts_too(self, monkeypatch, groq_only, status, body):
        seen = []
        mock_http(monkeypatch, self.groq_handler(seen, status=status, body=body))
        assert (await LLMClient().complete_json("q", TimeoutSchema)).answer == "ok"
        assert seen == ["test-groq-model", "groq-fallback"]

    @pytest.mark.asyncio
    async def test_both_models_dead_moves_on_to_the_next_provider_then_skips_groq(self, monkeypatch, groq_only):
        monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")
        seen = []
        client = LLMClient()
        client.report_retired = lambda *args: None
        mock_http(monkeypatch, self.groq_handler(seen, dead=("test-groq-model", "groq-fallback")))
        assert (await client.complete_json("q", TimeoutSchema)).answer == "gemini"
        assert (await client.complete_json("q", TimeoutSchema)).answer == "gemini"
        assert seen == ["test-groq-model", "groq-fallback"]  # the second call makes no Groq request at all

    @pytest.mark.asyncio
    async def test_other_errors_do_not_use_the_fallback(self, monkeypatch, groq_only):
        seen = []
        client = LLMClient()
        mock_http(monkeypatch, self.groq_handler(seen, status=500, body="boom"))
        with pytest.raises(LLMError, match="Groq HTTP 500: boom"):
            await client.complete_json("q", TimeoutSchema)
        assert seen == ["test-groq-model"] and not client._dead

    def test_attention_item_once_per_day_per_model(self, db):
        from app.db.models import AttentionItem
        from app.llm.client import record_model_retired

        record_model_retired("groq", "old-model", "new-model")
        record_model_retired("groq", "old-model", "new-model")
        record_model_retired("groq", "other-model")
        items = db.query(AttentionItem).all()
        assert [i.payload["reason"] for i in items] == ["model retired: groq old-model", "model retired: groq other-model"]
        assert items[0].payload["fallback"] == "new-model" and items[0].type.value == "needs_manual"

    def test_ping_uses_the_fallback_and_test_llm_names_the_model_used(self, monkeypatch, groq_only, capsys):
        seen = []
        from app.llm.client import llm_client

        monkeypatch.setattr(llm_client, "report_retired", lambda *args: None)
        monkeypatch.setattr(llm_client, "_dead", {})  # the shared client must not keep this test's dead model
        mock_http(monkeypatch, self.groq_handler(seen))
        client = LLMClient()
        client.report_retired = lambda *args: None
        assert asyncio.run(client.ping("groq")) == "groq-fallback"
        assert cli.main(["test-llm"]) == 0
        out = capsys.readouterr().out
        assert "OK groq (groq-fallback)" in out and "configured model test-groq-model is retired" in out

    def test_doctor_shows_the_fallback_and_the_model_last_used(self, at_head, monkeypatch):
        from app.llm.client import save_model_used

        monkeypatch.setattr(settings, "groq_model_fallback", "groq-fallback")
        save_model_used("groq", "groq-fallback")
        detail = rows_by_name()["LLM Groq"][1]
        assert "fallback groq-fallback" in detail and "last used groq-fallback" in detail
