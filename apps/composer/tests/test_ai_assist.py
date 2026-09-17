"""Tests for the composer AI Assist panel + endpoint (F-2.1 / F-5.3).

Provider HTTP calls are mocked at apps.credentials.ai_providers.httpx so no
network (or real API spend) is involved.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.credentials.models import AIProviderConfig
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class ComposerAIAssistTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.client.force_login(self.user)
        self.url = reverse("composer:ai_assist", kwargs={"workspace_id": self.workspace.id})


class ComposePanelVisibilityTests(ComposerAIAssistTestCase):
    """The composer shows a clear "add a key" state when no provider is configured."""

    def _compose_html(self):
        url = reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_shows_configure_prompt_without_provider(self):
        html = self._compose_html()
        self.assertIn("Add your API key in Organization Settings", html)
        self.assertNotIn("Generate Caption", html)

    def test_shows_actions_with_provider_configured(self):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-test", model="gpt-4o-mini", is_default=True
        )
        html = self._compose_html()
        self.assertIn("Generate Caption", html)
        self.assertNotIn("Add your API key in Organization Settings", html)


class AIAssistEndpointTests(ComposerAIAssistTestCase):
    def test_no_provider_configured_returns_clear_error(self):
        response = self.client.post(self.url, {"ai_action": "generate_caption", "prompt": "new latte"})
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data["error"], "no_provider")
        self.assertIn("Organization Settings", data["message"])

    def test_unknown_action_returns_400(self):
        AIProviderConfig.objects.create(organization=self.org, provider="openai", api_key="sk-test", model="gpt-4o-mini")
        response = self.client.post(self.url, {"ai_action": "translate"})
        self.assertEqual(response.status_code, 400)

    def test_generate_caption_missing_prompt_returns_400(self):
        AIProviderConfig.objects.create(organization=self.org, provider="openai", api_key="sk-test", model="gpt-4o-mini")
        response = self.client.post(self.url, {"ai_action": "generate_caption", "prompt": ""})
        self.assertEqual(response.status_code, 400)

    @patch("apps.credentials.ai_providers.httpx.post")
    def test_generate_caption_returns_variations(self, mock_post):
        AIProviderConfig.objects.create(
            organization=self.org, provider="openai", api_key="sk-test", model="gpt-4o-mini", is_default=True
        )
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "Caption one\nCaption two\nCaption three"}}]
        }
        mock_post.return_value = resp

        response = self.client.post(self.url, {"ai_action": "generate_caption", "prompt": "new latte"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["variations"],
            ["Caption one", "Caption two", "Caption three"],
        )
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-test")

    @patch("apps.credentials.ai_providers.httpx.post")
    def test_suggest_hashtags_uses_default_provider(self, mock_post):
        AIProviderConfig.objects.create(organization=self.org, provider="openai", api_key="sk-1", model="gpt-4o-mini")
        AIProviderConfig.objects.create(
            organization=self.org, provider="anthropic", api_key="sk-2", model="claude-haiku-4-5-20251001", is_default=True
        )
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"content": [{"text": "#coffee #latte #newmenu"}]}
        mock_post.return_value = resp

        response = self.client.post(self.url, {"ai_action": "suggest_hashtags", "caption": "New latte in store!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hashtags"], ["#coffee", "#latte", "#newmenu"])
        # The org's default provider (anthropic) is the one that got called.
        self.assertEqual(mock_post.call_args.args[0], "https://api.anthropic.com/v1/messages")

    def test_rewrite_tone_missing_caption_returns_400(self):
        AIProviderConfig.objects.create(organization=self.org, provider="openai", api_key="sk-test", model="gpt-4o-mini")
        response = self.client.post(self.url, {"ai_action": "rewrite_tone", "caption": "", "tone": "casual"})
        self.assertEqual(response.status_code, 400)

    @patch("apps.credentials.ai_providers.httpx.post")
    def test_provider_error_maps_to_502(self, mock_post):
        import httpx

        AIProviderConfig.objects.create(organization=self.org, provider="openai", api_key="sk-test", model="gpt-4o-mini")
        mock_post.side_effect = httpx.ConnectTimeout("timed out")

        response = self.client.post(self.url, {"ai_action": "generate_caption", "prompt": "new latte"})
        self.assertEqual(response.status_code, 502)
