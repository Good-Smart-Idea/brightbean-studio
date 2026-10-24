"""Tests for the org-settings AI Providers page (save / set default / delete / test)."""

from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.credentials.models import AIProviderConfig
from apps.members.models import OrgMembership


class AIProvidersSettingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="admin@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        # Every user gets a "My Organization" auto-provisioned on create (see
        # apps.accounts.signals) with a matching OrgMembership. Use that one
        # instead of creating a second Organization — RBACMiddleware resolves
        # ``request.org`` via ``OrgMembership.objects.filter(user=...).first()``,
        # so a second membership row would race with the auto-created one and
        # send requests to the wrong org (see apps/api_keys/tests/test_views.py
        # for the same pitfall).
        membership = self.user.org_memberships.first()
        membership.org_role = OrgMembership.OrgRole.ADMIN
        membership.save(update_fields=["org_role"])
        self.org = membership.organization
        self.client.force_login(self.user)
        self.url = reverse("credentials:ai_providers")

    def test_non_admin_gets_403(self):
        member = User.objects.create_user(
            email="member@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        # Downgrade their own auto-created membership to MEMBER — they don't
        # need to be in `self.org` at all, just somewhere without ADMIN.
        member_membership = member.org_memberships.first()
        member_membership.org_role = OrgMembership.OrgRole.MEMBER
        member_membership.save(update_fields=["org_role"])
        self.client.force_login(member)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_saving_first_provider_makes_it_default(self):
        response = self.client.post(
            self.url, {"action": "save", "provider": "openai", "api_key": "sk-test", "model": "gpt-4o-mini"}
        )
        self.assertEqual(response.status_code, 302)
        config = AIProviderConfig.objects.get(organization=self.org, provider="openai")
        self.assertEqual(config.api_key, "sk-test")
        self.assertTrue(config.is_default)

    def test_blank_api_key_keeps_existing_key(self):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-original", model="gpt-4o-mini", is_default=True
        )
        self.client.post(self.url, {"action": "save", "provider": "openai", "api_key": "", "model": "gpt-4o"})
        config = AIProviderConfig.objects.get(organization=self.org, provider="openai")
        self.assertEqual(config.api_key, "sk-original")
        self.assertEqual(config.model, "gpt-4o")

    def test_set_default_switches_default(self):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="k1", model="gpt-4o-mini", is_default=True
        )
        anthropic = AIProviderConfig.objects.create(
            organization=self.org, provider="anthropic", api_key="k2", model="claude-haiku-4-5-20251001"
        )
        self.client.post(self.url, {"action": "set_default", "provider": "anthropic"})
        anthropic.refresh_from_db()
        openai_config = AIProviderConfig.objects.get(organization=self.org, provider="openai")
        self.assertTrue(anthropic.is_default)
        self.assertFalse(openai_config.is_default)

    def test_delete_removes_provider(self):
        AIProviderConfig.objects.create(organization=self.org, provider="openai", api_key="k1", model="gpt-4o-mini")
        self.client.post(self.url, {"action": "delete", "provider": "openai"})
        self.assertFalse(AIProviderConfig.objects.filter(organization=self.org, provider="openai").exists())

    def test_delete_default_promotes_another_provider(self):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="k1", model="gpt-4o-mini", is_default=True
        )
        other = AIProviderConfig.objects.create(
            organization=self.org, provider="anthropic", api_key="k2", model="claude-haiku-4-5-20251001"
        )
        self.client.post(self.url, {"action": "delete", "provider": "openai"})
        other.refresh_from_db()
        self.assertTrue(other.is_default)
        self.assertFalse(AIProviderConfig.objects.filter(organization=self.org, provider="openai").exists())

    def test_save_clears_previous_test_result(self):
        AIProviderConfig.objects.create(
            organization=self.org,
            provider="openai",
            api_key="sk-original",
            model="gpt-4o-mini",
            is_default=True,
            test_result=AIProviderConfig.TestResult.SUCCESS,
            test_message="OK (gpt-4o-mini)",
            tested_at=timezone.now(),
        )
        self.client.post(self.url, {"action": "save", "provider": "openai", "api_key": "", "model": "gpt-4o"})
        config = AIProviderConfig.objects.get(organization=self.org, provider="openai")
        self.assertEqual(config.api_key, "sk-original")
        self.assertEqual(config.model, "gpt-4o")
        self.assertEqual(config.test_result, AIProviderConfig.TestResult.UNTESTED)
        self.assertEqual(config.test_message, "")
        self.assertIsNone(config.tested_at)

    def test_test_connection_records_success_and_failure(self):
        config = AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-a", model="gpt-4o-mini"
        )
        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status = MagicMock()
        ok.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
        with patch("apps.credentials.ai_providers.httpx.post", return_value=ok):
            response = self.client.post(self.url, {"action": "test", "provider": "openai"})
        self.assertEqual(response.status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.test_result, AIProviderConfig.TestResult.SUCCESS)
        self.assertIn("gpt-4o-mini", config.test_message)
        self.assertNotIn("sk-a", config.test_message)
        self.assertIsNotNone(config.tested_at)

        bad = MagicMock()
        bad.status_code = 401
        with patch("apps.credentials.ai_providers.httpx.post", return_value=bad):
            self.client.post(self.url, {"action": "test", "provider": "openai"})
        config.refresh_from_db()
        self.assertEqual(config.test_result, AIProviderConfig.TestResult.FAILURE)
        self.assertIn("API key", config.test_message)
        self.assertNotIn("sk-a", config.test_message)

    def test_test_connection_without_config(self):
        response = self.client.post(self.url, {"action": "test", "provider": "openrouter"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AIProviderConfig.objects.filter(organization=self.org).count(), 0)

    def test_settings_page_masks_api_key_and_offers_test(self):
        AIProviderConfig.objects.create(
            organization=self.org,
            provider="openrouter",
            api_key="sk-totally-secret",
            model="anthropic/claude-sonnet-4",
            test_result=AIProviderConfig.TestResult.SUCCESS,
            test_message="OK (anthropic/claude-sonnet-4)",
        )
        html = self.client.get(self.url).content.decode()
        self.assertIn("****cret", html)
        self.assertNotIn("sk-totally-secret", html)
        self.assertIn("Test connection", html)
        self.assertIn("OpenRouter", html)
        self.assertIn("OK (anthropic/claude-sonnet-4)", html)
