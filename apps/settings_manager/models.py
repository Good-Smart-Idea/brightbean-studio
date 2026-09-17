import uuid

from django.db import models

from apps.common.encryption import EncryptedTextField
from apps.common.managers import OrgScopedManager


class OrgSetting(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="settings",
    )
    key = models.CharField(max_length=255)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "settings_org_setting"
        unique_together = [("organization", "key")]

    def __str__(self):
        return f"{self.organization.name}: {self.key}"


class WorkspaceSetting(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="settings",
    )
    key = models.CharField(max_length=255)
    value = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "settings_workspace_setting"
        unique_together = [("workspace", "key")]

    def __str__(self):
        return f"{self.workspace.name}: {self.key}"


class AIProviderConfig(models.Model):
    """Org-supplied (BYOK) AI provider credentials - F-5.3.

    Each org may configure one row per provider (OpenAI, Anthropic,
    OpenRouter) with its own API key, encrypted at rest with the same
    AES-256-GCM field encryption used for platform credentials. Exactly one
    configured provider is the org-wide default used by AI features.
    """

    class Provider(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        ANTHROPIC = "anthropic", "Anthropic"
        OPENROUTER = "openrouter", "OpenRouter"

    class TestResult(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILURE = "failure", "Failure"
        UNTESTED = "untested", "Untested"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="ai_provider_configs",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    api_key = EncryptedTextField(help_text="Org-supplied API key, encrypted at rest")
    default_model = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    tested_at = models.DateTimeField(blank=True, null=True)
    test_result = models.CharField(max_length=20, choices=TestResult.choices, default=TestResult.UNTESTED)
    test_message = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "settings_ai_provider_config"
        unique_together = [("organization", "provider")]

    def __str__(self):
        return f"{self.organization.name} - {self.get_provider_display()} ({self.default_model})"

    def save(self, *args, **kwargs):
        # Ensure at most one default per org.
        if self.is_default:
            AIProviderConfig.objects.filter(organization=self.organization).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    @property
    def masked_api_key(self):
        key = self.api_key or ""
        return "****" + key[-4:] if len(key) > 4 else "****"

    def is_configured(self):
        return bool(self.is_active and (self.api_key or "").strip() and (self.default_model or "").strip())
