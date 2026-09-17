"""Composer AI Assist (F-2.1 / F-5.3) - provider abstraction.

One small function, ``generate_text(provider, api_key, prompt, model)``,
plus thin per-provider HTTP calls and prompt builders for the three composer
actions (generate caption, suggest hashtags, rewrite for tone). No plugin
framework - three providers, three branches.

All provider traffic goes through ``httpx`` so tests can mock
``apps.composer.ai.httpx`` the same way Unsplash tests do.
"""

import logging
import re

import httpx
from django.utils import timezone

logger = logging.getLogger(__name__)


class AIProviderError(Exception):
    """Raised when an AI provider call fails (bad key, quota, timeout...)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


OPENAI_API_BASE = "https://api.openai.com/v1"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

REQUEST_TIMEOUT = 60.0

# Suggested models per provider, used as dropdown hints in settings (F-5.3).
MODEL_SUGGESTIONS = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o3-mini"],
    "anthropic": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
    # OpenRouter models are free-text identifiers (e.g. "anthropic/claude-sonnet-4").
    "openrouter": [],
}


def _call_openai(base_url, api_key, system, prompt, model):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800,
    }
    if system:
        body["messages"].insert(0, {"role": "system", "content": system})
    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise AIProviderError("Could not reach the AI provider. Try again.") from exc
    if resp.status_code == 401 or resp.status_code == 403:
        raise AIProviderError(
            "The API key was rejected by the provider. Check it in Organization Settings.", resp.status_code
        )
    if resp.status_code == 429:
        raise AIProviderError("The AI provider rate limit was reached. Try again shortly.", resp.status_code)
    if resp.status_code != 200:
        raise AIProviderError("The AI provider request failed. Try again.", resp.status_code)
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("Unexpected response from the AI provider. Try again.", resp.status_code) from exc


def _call_anthropic(api_key, system, prompt, model):
    body = {
        "model": model,
        "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    try:
        resp = httpx.post(
            f"{ANTHROPIC_API_BASE}/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise AIProviderError("Could not reach the AI provider. Try again.") from exc
    if resp.status_code in (401, 403):
        raise AIProviderError(
            "The API key was rejected by the provider. Check it in Organization Settings.", resp.status_code
        )
    if resp.status_code == 429:
        raise AIProviderError("The AI provider rate limit was reached. Try again shortly.", resp.status_code)
    if resp.status_code != 200:
        raise AIProviderError("The AI provider request failed. Try again.", resp.status_code)
    try:
        blocks = resp.json()["content"]
        text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
        if not text:
            raise ValueError("empty content")
        return text
    except (ValueError, KeyError, TypeError) as exc:
        raise AIProviderError("Unexpected response from the AI provider. Try again.", resp.status_code) from exc


def generate_text(provider, api_key, prompt, model=None, system=None):
    """Generate text from (provider, api_key, prompt) and return it as a string.

    ``provider`` is one of "openai", "anthropic", "openrouter". OpenRouter
    speaks the OpenAI-compatible chat completions protocol, so it reuses that
    call path with a different base URL. Raises ``AIProviderError`` on any
    failure so callers can surface a friendly message.
    """
    if not api_key:
        raise AIProviderError("No API key configured for this provider.")
    if provider == "openai":
        return _call_openai(OPENAI_API_BASE, api_key, system, prompt, model or "gpt-4o-mini")
    if provider == "anthropic":
        return _call_anthropic(api_key, system, prompt, model or "claude-haiku-4-5-20251001")
    if provider == "openrouter":
        return _call_openai(OPENROUTER_API_BASE, api_key, system, prompt, model or "")
    raise AIProviderError(f"Unknown provider: {provider}")


def test_provider_config(config):
    """Send a trivial prompt ("Reply with OK") to validate a config end to end.

    Returns (ok: bool, detail: str, latency_ms: int|None).
    """
    start = timezone.now()
    try:
        generate_text(config.provider, config.api_key, "Reply with OK", model=config.default_model)
    except AIProviderError as exc:
        return False, str(exc), None
    latency_ms = int((timezone.now() - start).total_seconds() * 1000)
    return True, f"OK ({config.default_model})", latency_ms


def resolve_ai_provider(org_id):
    """Return the org's default active AIProviderConfig, or None.

    Falls back to any active configured provider when no default is marked.
    """
    from apps.settings_manager.models import AIProviderConfig

    configs = [c for c in AIProviderConfig.objects.for_org(org_id).filter(is_active=True) if c.is_configured()]
    if not configs:
        return None
    for config in configs:
        if config.is_default:
            return config
    return configs[0]


# ---------------------------------------------------------------------------
# Prompt builders for the three composer actions (F-2.1 AI Assist).
# ---------------------------------------------------------------------------

AI_SYSTEM_PROMPT = (
    "You are a social media copywriting assistant inside a publishing tool. "
    "Follow the requested output format exactly. Do not add explanations."
)


def build_ai_prompt(action, text, tone=None):
    """Build (system, user prompt) for a composer AI action.

    ``action`` is "caption" (generate 3 variations from a prompt/topic),
    "hashtags" (suggest hashtags for existing text), or "rewrite" (rewrite
    existing text in a given tone).
    """
    text = (text or "").strip()
    if action == "caption":
        prompt = (
            f"Write 3 short social media caption variations for the following idea or "
            f"topic. Separate them with lines containing only '###'. No numbering, no "
            f"explanations.\n\nIdea/topic: {text}"
        )
        return AI_SYSTEM_PROMPT, prompt
    if action == "hashtags":
        prompt = (
            "Suggest 10-15 relevant social media hashtags for the following caption. "
            "Return only the hashtags, one per line, each starting with '#'.\n\nCaption:\n"
            + (text or "(no caption text)")
        )
        return AI_SYSTEM_PROMPT, prompt
    if action == "rewrite":
        tone = tone or "professional"
        prompt = (
            f'Rewrite the following caption in a "{tone}" tone. Return only the rewritten caption.\n\nCaption:\n{text}'
        )
        return AI_SYSTEM_PROMPT, prompt
    raise ValueError(f"Unknown AI action: {action}")


def split_variations(raw):
    """Split a caption-generation response into variation strings."""
    parts = [p.strip() for p in re.split(r"^###\s*$", (raw or "").strip(), flags=re.MULTILINE) if p.strip()]
    return parts


def parse_hashtags(raw):
    """Extract hashtags from a hashtag-generation response."""
    return sorted({tag for tag in re.findall(r"#\w+", raw or "")})


TONES = [
    ("professional", "Professional"),
    ("casual", "Casual"),
    ("humorous", "Humorous"),
    ("inspirational", "Inspirational"),
    ("urgent", "Urgent"),
]
