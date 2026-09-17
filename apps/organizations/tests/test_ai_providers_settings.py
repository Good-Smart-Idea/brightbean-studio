"""Tests for the org-settings AI Providers (BYOK) section - F-5.3.

Provider HTTP calls are mocked at apps.composer.ai.httpx; no network.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership
from apps.settings_manager.models import AIProviderConfig


class AIProvidersSettingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="admin@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        # User creation auto-provisions a default org + membership; middleware
        # resolves request.org from the user's FIRST membership, so tests must
        # use that org rather than creating a second one.
        self.org = OrgMembership.objects.filter(user=self.user).select_related("organization").first().organization
        self.user_org_membership = OrgMembership.objects.filter(user=self.user).first()
        self.client.force_login(self.user)
        self.url = reverse("organizations:settings")

    def _response_mock(self, content="OK"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={"choices": [{"message": {"content": content}}]})
        return resp

    def test_settings_page_shows_ai_providers_section(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("AI Providers", html)
        self.assertIn("encrypted at rest", html)
        for label in ("OpenAI", "Anthropic", "OpenRouter"):
            self.assertIn(label, html)

    def test_save_ai_provider_creates_encrypted_config(self):
        response = self.client.post(
            self.url,
            {
                "action": "save_ai_provider",
                "provider": "openai",
                "api_key": "sk-live-key-42",
                "default_model": "gpt-4o-mini",
                "make_default": "on",
            },
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        config = AIProviderConfig.objects.for_org(self.org.id).get(provider="openai")
        self.assertEqual(config.default_model, "gpt-4o-mini")
        self.assertTrue(config.is_default)
        # Plaintext key round-trips through the encrypted field, and is masked in UI.
        self.assertEqual(config.api_key, "sk-live-key-42")
        self.assertIn("****", config.masked_api_key)

    def test_save_ai_provider_rejects_unknown_provider(self):
        self.client.post(
            self.url,
            {"action": "save_ai_provider", "provider": "gemini", "api_key": "k", "default_model": "m"},
        )
        self.assertEqual(AIProviderConfig.objects.for_org(self.org.id).count(), 0)

    def test_save_keeps_existing_key_when_field_blank(self):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-original", default_model="gpt-4o-mini"
        )
        self.client.post(
            self.url,
            {"action": "save_ai_provider", "provider": "openai", "api_key": "", "default_model": "gpt-4.1-mini"},
        )
        config = AIProviderConfig.objects.for_org(self.org.id).get(provider="openai")
        self.assertEqual(config.api_key, "sk-original")
        self.assertEqual(config.default_model, "gpt-4.1-mini")

    def test_save_requires_model(self):
        self.client.post(
            self.url, {"action": "save_ai_provider", "provider": "openai", "api_key": "sk-x", "default_model": ""}
        )
        self.assertEqual(AIProviderConfig.objects.for_org(self.org.id).count(), 0)

    def test_set_default_moves_flag_between_providers(self):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-a", default_model="gpt-4o-mini", is_default=True
        )
        anthropic = AIProviderConfig.objects.create(
            organization=self.org, provider="anthropic", api_key="sk-b", default_model="claude-haiku-4-5-20251001"
        )
        self.client.post(self.url, {"action": "set_default_ai_provider", "provider": "anthropic"})
        anthropic.refresh_from_db()
        self.assertTrue(anthropic.is_default)
        self.assertFalse(AIProviderConfig.objects.for_org(self.org.id).get(provider="openai").is_default)

    def test_test_action_reports_success_and_failure(self):
        config = AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-a", default_model="gpt-4o-mini"
        )
        with patch("apps.composer.ai.httpx.post", return_value=self._response_mock()):
            self.client.post(self.url, {"action": "test_ai_provider", "provider": "openai"})
        config.refresh_from_db()
        self.assertEqual(config.test_result, AIProviderConfig.TestResult.SUCCESS)
        self.assertIn("gpt-4o-mini", config.test_message)

        with patch("apps.composer.ai.httpx.post", return_value=self._response_mock()) as post:
            post.return_value.status_code = 401
            post.return_value.json = MagicMock(return_value={})
            self.client.post(self.url, {"action": "test_ai_provider", "provider": "openai"})
        config.refresh_from_db()
        self.assertEqual(config.test_result, AIProviderConfig.TestResult.FAILURE)
        self.assertIn("API key", config.test_message)

    def test_delete_removes_config_and_reassigns_default(self):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-a", default_model="gpt-4o-mini", is_default=True
        )
        remaining = AIProviderConfig.objects.create(
            organization=self.org, provider="anthropic", api_key="sk-b", default_model="claude-haiku-4-5-20251001"
        )
        self.client.post(self.url, {"action": "delete_ai_provider", "provider": "openai"})
        self.assertEqual(AIProviderConfig.objects.for_org(self.org.id).count(), 1)
        remaining.refresh_from_db()
        self.assertTrue(remaining.is_default)

    def test_settings_view_masks_keys_on_page(self):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-totally-secret", default_model="gpt-4o-mini"
        )
        html = self.client.get(self.url).content.decode()
        self.assertIn("****", html)
        self.assertNotIn("sk-totally-secret", html)
