"""Tests for the composer AI Assist panel and generation endpoints.

The AI provider HTTP traffic is mocked at apps.composer.ai.httpx (see
test_ai_providers.py); these tests only exercise views/templates.
"""

from unittest.mock import MagicMock, patch

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.tests.test_unsplash import ComposerTestCase
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.settings_manager.models import AIProviderConfig
from apps.workspaces.models import Workspace


def _make_org_user():
    user = User.objects.create_user(
        email="ai-owner@example.com",
        password="testpass123",
        tos_accepted_at=timezone.now(),
    )
    org = Organization.objects.create(name="AI Org")
    workspace = Workspace.objects.create(organization=org, name="AI Workspace")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user, org, workspace


def _config(org, **kwargs):
    defaults = {
        "provider": "openai",
        "api_key": "sk-test-key",
        "default_model": "gpt-4o-mini",
        "is_default": True,
    }
    defaults.update(kwargs)
    return AIProviderConfig.objects.create(organization=org, **defaults)


class AiPanelTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("composer:ai_panel", kwargs={"workspace_id": self.workspace.id})

    def test_unconfigured_state_prompts_to_add_key(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("AI Assist isn't set up yet", html)
        self.assertIn("Organization Settings", html)
        # Must render the settings link, not a broken button.
        self.assertIn(reverse("organizations:settings"), html)

    def test_configured_state_renders_actions(self):
        _config(self.org)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Generate caption", html)
        self.assertIn("Suggest hashtags", html)
        self.assertIn("Rewrite for tone", html)
        self.assertIn("OpenAI · gpt-4o-mini", html)
        self.assertIn(reverse("composer:ai_generate", kwargs={"workspace_id": self.workspace.id}), html)
        self.assertNotIn("set up yet", html)

    def test_requires_membership(self):
        other_user = User.objects.create_user(
            email="outsider@example.com", password="x", tos_accepted_at=timezone.now()
        )
        self.client.force_login(other_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)


class AiComposePageTests(ComposerTestCase):
    def test_compose_page_renders_ai_assist_button(self):
        url = reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('title="AI Assist"', html)
        self.assertIn('x-show="showAiPanel"', html)

    def test_compose_page_ai_panel_htmx_url_present(self):
        _config(self.org)
        url = reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})
        response = self.client.get(url)
        html = response.content.decode()
        self.assertIn(reverse("composer:ai_panel", kwargs={"workspace_id": self.workspace.id}), html)


class AiGenerateTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("composer:ai_generate", kwargs={"workspace_id": self.workspace.id})

    def test_returns_409_without_configured_provider(self):
        response = self.client.post(self.url, {"action": "caption", "text": "coffee promo"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("No AI provider configured", response.json()["error"])

    @patch("apps.composer.ai.httpx.post")
    def test_caption_returns_three_variations(self, post):
        _config(self.org)
        post.return_value = MagicMock(status_code=200)
        post.return_value.json = MagicMock(
            return_value={"choices": [{"message": {"content": "One\n###\nTwo\n###\nThree"}}]}
        )
        response = self.client.post(self.url, {"action": "caption", "text": "coffee promo"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["variations"], ["One", "Two", "Three"])

    @patch("apps.composer.ai.httpx.post")
    def test_hashtags_returns_parsed_list(self, post):
        _config(self.org, provider="anthropic", default_model="claude-haiku-4-5-20251001")
        post.return_value = MagicMock(status_code=200)
        post.return_value.json = MagicMock(return_value={"content": [{"text": "#coffee #latte #springmenu #coffee"}]})
        response = self.client.post(self.url, {"action": "hashtags", "text": "New spring menu!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hashtags"], ["#coffee", "#latte", "#springmenu"])

    @patch("apps.composer.ai.httpx.post")
    def test_rewrite_returns_text(self, post):
        _config(self.org)
        post.return_value = MagicMock(status_code=200)
        post.return_value.json = MagicMock(
            return_value={"choices": [{"message": {"content": "Totally grab a latte!"}}]}
        )
        response = self.client.post(self.url, {"action": "rewrite", "text": "Buy a latte", "tone": "casual"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "Totally grab a latte!")

    @patch("apps.composer.ai.httpx.post")
    def test_provider_error_maps_to_502(self, post):
        _config(self.org)
        post.return_value = MagicMock(status_code=401)
        post.return_value.json = MagicMock(return_value={"error": {"message": "bad key"}})
        response = self.client.post(self.url, {"action": "caption", "text": "coffee promo"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("API key", response.json()["error"])

    def test_unknown_action_is_400(self):
        _config(self.org)
        response = self.client.post(self.url, {"action": "translate", "text": "hello"})
        self.assertEqual(response.status_code, 400)

    def test_missing_text_is_400(self):
        _config(self.org)
        response = self.client.post(self.url, {"action": "caption", "text": "  "})
        self.assertEqual(response.status_code, 400)

    def test_rewrite_requires_valid_tone(self):
        _config(self.org)
        response = self.client.post(self.url, {"action": "rewrite", "text": "hello", "tone": "pirate"})
        self.assertEqual(response.status_code, 400)
