"""Org-settings self-service page for BYOK AI provider configuration (F-5.3).

One page listing the three supported providers (OpenAI, Anthropic,
OpenRouter); an org configures any subset and marks one as default. Mirrors
the org-settings pattern used by ``apps.organizations.views.settings_view``
(``@require_org_role(ADMIN)``, POST ``action`` dispatch, redirect back to
the same page).
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.members.decorators import require_org_role
from apps.members.models import OrgMembership

from .forms import AI_MODEL_SUGGESTIONS, AIProviderConfigForm
from .models import AIProviderConfig


@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_http_methods(["GET", "POST"])
def ai_providers_view(request):
    org = request.org
    configs = {c.provider: c for c in AIProviderConfig.objects.filter(organization=org)}

    if request.method == "POST":
        action = request.POST.get("action")
        provider = request.POST.get("provider")
        if action == "delete" and provider in configs:
            configs[provider].delete()
            messages.success(request, "Removed AI provider.")
            return redirect("credentials:ai_providers")
        if action == "set_default" and provider in configs:
            configs[provider].is_default = True
            configs[provider].save(update_fields=["is_default", "updated_at"])
            messages.success(request, "Default AI provider updated.")
            return redirect("credentials:ai_providers")
        if action == "save" and provider in dict(AIProviderConfig.Provider.choices):
            existing = configs.get(provider)
            # Captured before is_valid()/save(): ModelForm binds to ``existing``
            # in place, so by the time the form has run, existing.api_key has
            # already been overwritten with the (possibly blank) posted value.
            original_api_key = existing.api_key if existing else None
            form = AIProviderConfigForm(
                request.POST,
                instance=existing,
                existing_key_set=bool(existing),
            )
            if form.is_valid():
                config = form.save(commit=False)
                if not form.cleaned_data["api_key"]:
                    # Blank means "keep the existing key" — don't overwrite it.
                    config.api_key = original_api_key
                config.organization = org
                config.provider = provider
                # First provider configured for the org becomes the default
                # automatically so AI Assist has something to use immediately.
                if not existing and not configs:
                    config.is_default = True
                config.save()
                messages.success(request, f"Saved {config.get_provider_display()} configuration.")
                return redirect("credentials:ai_providers")
            messages.error(request, "Could not save provider — check the fields below.")
            return _render(request, org, configs, error_provider=provider, error_form=form)

    return _render(request, org, configs)


def _render(request, org, configs, error_provider=None, error_form=None):
    rows = []
    for value, label in AIProviderConfig.Provider.choices:
        existing = configs.get(value)
        if error_provider == value:
            form = error_form
        else:
            form = AIProviderConfigForm(instance=existing, existing_key_set=bool(existing))
            form.fields["provider"].initial = value
        rows.append(
            {
                "provider": value,
                "label": label,
                "config": existing,
                "form": form,
                "model_suggestions": AI_MODEL_SUGGESTIONS.get(value, []),
            }
        )
    context = {
        "organization": org,
        "settings_active": "ai_providers",
        "rows": rows,
        "has_any_provider": bool(configs),
    }
    return render(request, "credentials/ai_providers.html", context)
