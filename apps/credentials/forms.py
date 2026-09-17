from django import forms
from django.utils.safestring import mark_safe

from .models import REQUIRED_CREDENTIAL_KEYS, AIProviderConfig, PlatformCredential, derive_is_configured

AI_MODEL_SUGGESTIONS = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o3-mini"],
    "anthropic": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
    "openrouter": [],  # free-text — OpenRouter hosts hundreds of models
}

_AI_INPUT_CLASSES = "input-focus w-full px-3 py-2 text-sm rounded-lg outline-none transition-colors"
_AI_INPUT_STYLE = "border: 1px solid var(--border); color: var(--text-primary); background: var(--surface-0);"


class AIProviderConfigForm(forms.ModelForm):
    """Org-settings self-service form for one AI provider's BYOK key + model."""

    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "off", "class": _AI_INPUT_CLASSES, "style": _AI_INPUT_STYLE},
        ),
        help_text="Leave blank to keep the existing key.",
    )

    class Meta:
        model = AIProviderConfig
        # ``is_default`` is deliberately excluded — it's toggled by the
        # dedicated "Make default" action, not this save form, so saving an
        # existing default provider's key/model can't accidentally clear it.
        fields = ("provider", "api_key", "model")
        widgets = {
            "provider": forms.HiddenInput(),
            "model": forms.TextInput(attrs={"class": _AI_INPUT_CLASSES, "style": _AI_INPUT_STYLE}),
        }

    def __init__(self, *args, existing_key_set=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._existing_key_set = existing_key_set
        self.fields["api_key"].required = not existing_key_set

    def clean_model(self):
        model = (self.cleaned_data.get("model") or "").strip()
        if not model:
            raise forms.ValidationError("A model is required.")
        return model

    def clean_api_key(self):
        key = (self.cleaned_data.get("api_key") or "").strip()
        if not key and not self._existing_key_set:
            raise forms.ValidationError("An API key is required.")
        return key

# Human-readable required-key hints, shown under the credentials field in the
# admin. Mirrors what each provider reads (see providers/*.py).
_KEY_HINTS = {
    "facebook": "client_id, client_secret (app_id / app_secret also accepted)",
    "instagram": "client_id, client_secret (app_id / app_secret also accepted)",
    "instagram_login": "client_id, client_secret (app_id / app_secret also accepted)",
    "threads": "client_id, client_secret (app_id / app_secret also accepted)",
    "pinterest": "client_id, client_secret (app_id / app_secret also accepted)",
    "tiktok": "client_key, client_secret  (note: client_key, NOT client_id)",
    "youtube": "client_id, client_secret",
    "google_business": "client_id, client_secret (optional: account_id, location_id)",
    "linkedin_personal": "client_id, client_secret (optional: _oauth_mode = oidc | community_management)",
    "linkedin_company": "client_id, client_secret",
}

CREDENTIALS_HELP = mark_safe(
    'JSON object of app credentials, e.g. <code>{"client_id": "...", "client_secret": "..."}</code>. '
    "Required keys per platform:<br>"
    + "<br>".join(f"<b>{platform}</b>: {hint}" for platform, hint in _KEY_HINTS.items())
)


class PlatformCredentialAdminForm(forms.ModelForm):
    # Override the EncryptedJSONField (a bare TextField) with a real JSON field so
    # the admin renders/parses proper JSON instead of a Python repr — without this
    # a no-edit save would store a corrupt JSON-encoded string.
    credentials = forms.JSONField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 8, "cols": 60}),
        help_text=CREDENTIALS_HELP,
    )

    class Meta:
        model = PlatformCredential
        fields = ("organization", "platform", "credentials")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only offer platforms that take app-level credentials (exclude session /
        # per-instance auth platforms like bluesky and mastodon).
        self.fields["platform"].choices = [
            choice
            for choice in self.fields["platform"].choices
            if choice[0] == "" or choice[0] in REQUIRED_CREDENTIAL_KEYS
        ]

    def clean_credentials(self):
        data = self.cleaned_data.get("credentials")
        if data in (None, ""):
            return {}
        if not isinstance(data, dict):
            raise forms.ValidationError(
                'Credentials must be a JSON object, e.g. {"client_id": "...", "client_secret": "..."}.'
            )
        cleaned = {}
        for key, value in data.items():
            if value is None:
                continue
            text = value if isinstance(value, str) else str(value)
            if text.strip():
                cleaned[key] = text
        return cleaned

    def clean(self):
        cleaned = super().clean()
        platform = cleaned.get("platform")
        credentials = cleaned.get("credentials") or {}
        if platform and credentials and not derive_is_configured(platform, credentials):
            required = REQUIRED_CREDENTIAL_KEYS.get(platform, ())
            hint = ", ".join(" / ".join(group) for group in required)
            self.add_error(
                "credentials",
                f"Missing required keys for {platform}. Expected: {hint or 'none'}. "
                "Fill in every required key — the row stays inactive until they are all present.",
            )
        return cleaned
