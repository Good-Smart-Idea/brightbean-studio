"""Tests for the Composer AI Assist provider abstraction (F-5.3 BYOK).

Provider HTTP traffic is mocked at apps.composer.ai.httpx so no network is
involved and no real API keys are spent.
"""

from unittest.mock import MagicMock, patch

import pytest

from apps.composer import ai as ai_module
from apps.settings_manager.models import AIProviderConfig


def _response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data or {})
    return resp


# ---------------------------------------------------------------------------
# generate_text (pure abstraction, mocked HTTP)
# ---------------------------------------------------------------------------


def test_openai_success():
    resp = _response(json_data={"choices": [{"message": {"content": "Hello!"}}]})
    with patch("apps.composer.ai.httpx.post", return_value=resp) as post:
        out = ai_module.generate_text("openai", "sk-test", "Hi", model="gpt-4o-mini")
    assert out == "Hello!"
    args, kwargs = post.call_args
    assert args[0] == "https://api.openai.com/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["json"]["model"] == "gpt-4o-mini"
    messages = kwargs["json"]["messages"]
    assert messages[-1] == {"role": "user", "content": "Hi"}
    # No system prompt passed: only the user message is sent.
    assert len(messages) == 1


def test_openai_default_model():
    resp = _response(json_data={"choices": [{"message": {"content": "x"}}]})
    with patch("apps.composer.ai.httpx.post", return_value=resp) as post:
        ai_module.generate_text("openai", "sk-test", "Hi")
    assert post.call_args[1]["json"]["model"] == "gpt-4o-mini"


def test_anthropic_success_concatenates_text_blocks():
    resp = _response(json_data={"content": [{"type": "text", "text": "Hei"}, {"type": "text", "text": "mo"}]})
    with patch("apps.composer.ai.httpx.post", return_value=resp) as post:
        out = ai_module.generate_text("anthropic", "sk-test", "Hi", model="claude-haiku-4-5-20251001")
    assert out == "Heimo"
    args, kwargs = post.call_args
    assert args[0] == "https://api.anthropic.com/v1/messages"
    assert kwargs["headers"]["x-api-key"] == "sk-test"
    assert kwargs["headers"]["anthropic-version"] == "2023-06-01"
    assert kwargs["json"]["model"] == "claude-haiku-4-5-20251001"
    assert kwargs["json"]["messages"][0]["content"] == "Hi"


def test_anthropic_system_prompt_via_top_level_field():
    resp = _response(json_data={"content": [{"text": "ok"}]})
    with patch("apps.composer.ai.httpx.post", return_value=resp) as post:
        ai_module.generate_text("anthropic", "k", "Hi", system="sys prompt")
    assert post.call_args[1]["json"]["system"] == "sys prompt"


def test_openrouter_uses_openai_compatible_base():
    resp = _response(json_data={"choices": [{"message": {"content": "hey"}}]})
    with patch("apps.composer.ai.httpx.post", return_value=resp) as post:
        out = ai_module.generate_text("openrouter", "sk-or", "Hi", model="anthropic/claude-sonnet-4")
    assert out == "hey"
    assert post.call_args[0][0] == "https://openrouter.ai/api/v1/chat/completions"
    assert post.call_args[1]["json"]["model"] == "anthropic/claude-sonnet-4"


def test_missing_api_key_raises_without_calling_api():
    with patch("apps.composer.ai.httpx.post") as post, pytest.raises(ai_module.AIProviderError):
        ai_module.generate_text("openai", "", "Hi")
    post.assert_not_called()


def test_unknown_provider_raises():
    with pytest.raises(ai_module.AIProviderError):
        ai_module.generate_text("gemini", "k", "Hi")


@pytest.mark.parametrize("status", [401, 403])
def test_openai_bad_key_is_friendly_error(status):
    with (
        patch("apps.composer.ai.httpx.post", return_value=_response(status)),
        pytest.raises(ai_module.AIProviderError) as excinfo,
    ):
        ai_module.generate_text("openai", "sk-bad", "Hi")
    assert "API key" in str(excinfo.value)
    assert excinfo.value.status == status


def test_openai_rate_limit_maps_to_429_message():
    with (
        patch("apps.composer.ai.httpx.post", return_value=_response(429)),
        pytest.raises(ai_module.AIProviderError) as excinfo,
    ):
        ai_module.generate_text("openai", "sk-test", "Hi")
    assert excinfo.value.status == 429


def test_openai_malformed_body_raises_friendly_error():
    with (
        patch("apps.composer.ai.httpx.post", return_value=_response(json_data={"unexpected": True})),
        pytest.raises(ai_module.AIProviderError),
    ):
        ai_module.generate_text("openai", "sk-test", "Hi")


def test_network_error_raises_friendly_error():
    import httpx

    with (
        patch("apps.composer.ai.httpx.post", side_effect=httpx.RequestError("boom")),
        pytest.raises(ai_module.AIProviderError),
    ):
        ai_module.generate_text("openai", "sk-test", "Hi")


# ---------------------------------------------------------------------------
# Prompt builders + parsers (pure functions, no DB)
# ---------------------------------------------------------------------------


def test_build_caption_prompt_asks_for_three_variations():
    system, prompt = ai_module.build_ai_prompt("caption", "coffee shop latte promo")
    assert "3" in prompt and "###" in prompt and "coffee shop latte promo" in prompt
    assert system


def test_build_hashtags_prompt():
    system, prompt = ai_module.build_ai_prompt("hashtags", "New spring menu is here!")
    assert "#\\w+" not in prompt and "10-15" in prompt


def test_build_rewrite_prompt_with_tone():
    system, prompt = ai_module.build_ai_prompt("rewrite", "Buy stuff", tone="humorous")
    assert "humorous" in prompt


def test_build_rewrite_defaults_to_professional_tone():
    _, prompt = ai_module.build_ai_prompt("rewrite", "Buy stuff")
    assert "professional" in prompt


def test_build_unknown_action_raises():
    with pytest.raises(ValueError):
        ai_module.build_ai_prompt("translate", "x")


def test_split_variations():
    raw = "First caption\n###\nSecond caption\n###\nThird caption"
    assert ai_module.split_variations(raw) == ["First caption", "Second caption", "Third caption"]
    assert ai_module.split_variations("") == []
    assert ai_module.split_variations("###\n###") == []


def test_parse_hashtags_dedupes():
    raw = "#coffee #coffee #latte\n#ButterLatte"
    assert ai_module.parse_hashtags(raw) == ["#ButterLatte", "#coffee", "#latte"]
    assert ai_module.parse_hashtags("no hashtags here") == []


# ---------------------------------------------------------------------------
# resolve_ai_provider + model save semantics
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolveAIProvider:
    def _org(self):
        from apps.organizations.models import Organization

        return Organization.objects.create(name="AI Org")

    def test_returns_none_when_nothing_configured(self):
        org = self._org()
        assert ai_module.resolve_ai_provider(org.id) is None

    def test_returns_default_provider(self):
        org = self._org()
        AIProviderConfig.objects.create(
            organization=org, provider="openai", api_key="sk-a", default_model="gpt-4o-mini", is_default=True
        )
        AIProviderConfig.objects.create(
            organization=org, provider="anthropic", api_key="sk-b", default_model="claude-haiku-4-5-20251001"
        )
        config = ai_module.resolve_ai_provider(org.id)
        assert config.provider == "openai"

    def test_falls_back_to_first_configured_without_default(self):
        org = self._org()
        AIProviderConfig.objects.create(
            organization=org, provider="anthropic", api_key="sk-b", default_model="claude-haiku-4-5-20251001"
        )
        config = ai_module.resolve_ai_provider(org.id)
        assert config.provider == "anthropic"

    def test_inactive_or_empty_configs_are_skipped(self):
        org = self._org()
        AIProviderConfig.objects.create(
            organization=org, provider="openai", api_key="", default_model="gpt-4o-mini", is_default=True
        )
        AIProviderConfig.objects.create(
            organization=org,
            provider="anthropic",
            api_key="sk-live",
            default_model="claude-haiku-4-5-20251001",
            is_active=False,
        )
        assert ai_module.resolve_ai_provider(org.id) is None


@pytest.mark.django_db
def test_only_one_default_per_org():
    from apps.organizations.models import Organization

    org = Organization.objects.create(name="Default Org")
    first = AIProviderConfig.objects.create(
        organization=org, provider="openai", api_key="sk-a", default_model="gpt-4o-mini", is_default=True
    )
    second = AIProviderConfig.objects.create(
        organization=org,
        provider="anthropic",
        api_key="sk-b",
        default_model="claude-haiku-4-5-20251001",
        is_default=True,
    )
    first.refresh_from_db()
    assert second.is_default is True
    assert first.is_default is False


@pytest.mark.django_db
def test_api_key_encrypted_at_rest_and_masked():
    from apps.organizations.models import Organization

    org = Organization.objects.create(name="Crypto Org")
    config = AIProviderConfig.objects.create(
        organization=org, provider="openai", api_key="sk-super-secret-123", default_model="gpt-4o-mini"
    )
    raw = AIProviderConfig.objects.filter(pk=config.pk).get().api_key  # decrypts through the field
    assert raw == "sk-super-secret-123"
    assert config.masked_api_key.startswith("****")
    assert "super-secret" not in config.masked_api_key

    # Raw column must not contain the plaintext key.
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT api_key FROM settings_ai_provider_config WHERE id = %s", [str(config.pk)])
        stored = cursor.fetchone()[0]
    assert "sk-super-secret-123" not in (stored or "")
