"""BYOK LLM provider abstraction for Composer AI Assist.

Three fixed providers, three small request builders — a plugin/registry
framework would be overkill for this list. Add a fourth branch here if a
fourth provider is ever needed.
"""

import httpx

_TIMEOUT = 30.0


class AIProviderError(Exception):
    """Raised when a provider call fails or returns an unusable response."""


def generate_text(provider, api_key, model, prompt):
    """Call ``provider``'s completion API with ``prompt`` and return the text.

    Raises ``AIProviderError`` on any transport, auth, or response-shape
    failure so callers don't need to know about httpx or each provider's
    JSON shape.
    """
    if provider == "openai":
        return _openai(api_key, model, prompt)
    if provider == "anthropic":
        return _anthropic(api_key, model, prompt)
    if provider == "openrouter":
        return _openrouter(api_key, model, prompt)
    raise AIProviderError(f"Unknown AI provider: {provider}")


def _openai(api_key, model, prompt):
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIProviderError(f"OpenAI request failed: {exc}") from exc


def _anthropic(api_key, model, prompt):
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}]},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIProviderError(f"Anthropic request failed: {exc}") from exc


def _openrouter(api_key, model, prompt):
    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIProviderError(f"OpenRouter request failed: {exc}") from exc
