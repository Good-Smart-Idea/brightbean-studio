"""BYOK LLM provider abstraction for Composer AI Assist.

Three fixed providers, three small request builders — a plugin/registry
framework would be overkill for this list. Add a fourth branch here if a
fourth provider is ever needed.
"""

import httpx
from django.utils import timezone

_TIMEOUT = 30.0

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class AIProviderError(Exception):
    """Raised when a provider call fails or returns an unusable response."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def generate_text(provider, api_key, model, prompt):
    """Call ``provider``'s completion API with ``prompt`` and return the text.

    Raises ``AIProviderError`` on any transport, auth, or response-shape
    failure so callers don't need to know about httpx or each provider's
    JSON shape. OpenRouter speaks the OpenAI chat-completions protocol, so
    it reuses that request shape with its own base URL. A blank model is
    rejected here — sending ``{"model": ""}`` is a 400 from OpenRouter.
    """
    if not (api_key or "").strip():
        raise AIProviderError("No API key configured for this provider.")
    if not (model or "").strip():
        raise AIProviderError("A model is required.")
    if provider == "openai":
        return _chat_completions(_OPENAI_URL, api_key, model, prompt, "OpenAI")
    if provider == "anthropic":
        return _anthropic(api_key, model, prompt)
    if provider == "openrouter":
        return _chat_completions(_OPENROUTER_URL, api_key, model, prompt, "OpenRouter")
    raise AIProviderError(f"Unknown AI provider: {provider}")


def test_provider_config(config):
    """Send ``Reply with OK`` to validate a stored config.

    Returns ``(ok, detail, latency_ms)``. ``detail`` names the model on
    success and the provider error on failure. It never includes the API key.
    """
    start = timezone.now()
    model = (config.model or "").strip()
    try:
        generate_text(config.provider, config.api_key, model, "Reply with OK")
    except AIProviderError as exc:
        return False, str(exc), None
    latency_ms = int((timezone.now() - start).total_seconds() * 1000)
    return True, f"OK ({model})", latency_ms


def _reject_auth_or_rate_limit(resp):
    """Turn 401/403/429 into a message that does not echo the key or body.

    Non-integer status codes (test doubles that leave ``status_code`` unset)
    fall through to ``raise_for_status`` so success-path mocks keep working.
    """
    status = resp.status_code
    if not isinstance(status, int):
        return
    if status in (401, 403):
        raise AIProviderError(
            "The API key was rejected by the provider. Check it in Organization Settings.",
            status,
        )
    if status == 429:
        raise AIProviderError(
            "The AI provider rate limit was reached. Try again shortly.",
            status,
        )


def _chat_completions(url, api_key, model, prompt, label):
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise AIProviderError(f"{label} request failed: {exc}") from exc
    _reject_auth_or_rate_limit(resp)
    try:
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIProviderError(f"{label} request failed: {exc}") from exc


def _anthropic(api_key, model, prompt):
    try:
        resp = httpx.post(
            _ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}]},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise AIProviderError(f"Anthropic request failed: {exc}") from exc
    _reject_auth_or_rate_limit(resp)
    try:
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIProviderError(f"Anthropic request failed: {exc}") from exc
