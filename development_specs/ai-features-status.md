# AI Features: Implementation Status

Written 2026-09-17 after Alex's "just a social media dashboard, nothing is AI
operated" complaint triggered a full audit. This is the ground truth on what
exists in code today, kept separate from `feature-spec-social-media-management-v2.md`
(which describes a BYOK AI Assist feature that was **never implemented** — see
below) so the next person doesn't have to re-derive this from scratch.

## Summary

| Feature | Spec'd? | Built? | Live/working? |
|---|---|---|---|
| Composer "AI Assist" (BYOK OpenAI/Anthropic/OpenRouter caption generation, tone rewrite) | Yes (F-2.1, F-5.3) | **No — 0% implemented** | No |
| `apps/intelligence` (hosted content-scoring/benchmarking SaaS add-on) | No (not in the v2 feature spec) | **Yes, fully built** | No — disabled, backend doesn't exist |
| Inbox "sentiment analysis" | Yes, described as AI-capable | Yes | Working, but keyword-based, not AI/LLM |

## 1. Composer AI Assist — spec-only, does not exist

`development_specs/feature-spec-social-media-management-v2.md` describes an
AI panel in the composer (caption generation, hashtag suggestions, tone
rewrite) backed by an org-configured OpenAI/Anthropic/OpenRouter API key
(`AIProviderConfig`, `AIDefaultProvider` models, an "AI Providers" settings
page). None of this exists: no such models, no migrations, no views, no
templates, no `openai`/`anthropic` SDK in `requirements.txt`. This explains
why the deployed container has zero LLM provider env vars — there is no code
path that would read one. This is not a missing-credential bug; it's an
unbuilt feature. Building it (encrypted per-org key storage, a provider
abstraction, the composer UI panel, streaming/generation endpoints) is a
real implementation project, not a config fix.

## 2. `apps/intelligence` — built, but the backend it calls doesn't exist anywhere

This app is a real, complete Studio-side client for an external paid
"Intelligence" microservice: content packaging scoring, video-hook scoring,
content-gap research, and channel/video benchmarking (YouTube-style content
analytics — not caption generation). It has models
(`IntelligenceSubscription`, `StudioCheckoutAttempt`, `PendingActivation`,
`IntelligenceUsageEvent`), an HMAC-signed internal client + a per-org bearer
API client (`apps/intelligence/services/client.py`), a Stripe-backed
subscribe/checkout flow, and a full URL surface (playground, tool endpoints,
billing portal).

It requires five env vars — `INTELLIGENCE_INTERNAL_URL`,
`INTELLIGENCE_PUBLIC_URL`, `STUDIO_DEPLOYMENT_ID`, `STUDIO_SHARED_SECRET`,
`STUDIO_BASE_URL` (`config/settings/base.py`). If any is empty,
`INTELLIGENCE_ENABLED` is `False` and the **entire** surface is hidden (no
nav item, no routes) — confirmed this fails closed, not with stub/fake data.

**Confirmed via infra audit: no "Intelligence" backend exists anywhere in
GSI's self-hosted infrastructure or GitHub orgs** (`Good-Smart-Idea`,
`brightbeanxyz`). It's not in the Ada `ops/ada-apps/` service catalog, and
`ops/ada-apps/brightbean/.env.example` explicitly documents these vars as
"Optional hosted Intelligence integration: all empty disables it" with the
provisioning script intentionally leaving them blank. There is no credential
to source here — the service itself was never built or deployed. Turning
this on means standing up a new microservice (its own AI scoring backend,
Stripe billing, HMAC-authenticated API) from scratch.

## 3. Inbox sentiment analysis — real, working, not AI

`apps/inbox/sentiment.py` is a plain keyword-matching engine (positive/
negative word lists), not an LLM call. It works correctly and templates
label it plainly as "sentiment" (not "AI sentiment"), so there's no
misleading claim in the UI. The v2 spec's "optional AI-based sentiment
analysis" would route through the same nonexistent AI-provider-key
infrastructure as item 1 above.

## Bottom line

There is no half-built, key-starved AI feature to "wire a credential into."
The product genuinely has zero LLM-backed features live today. The two paths
forward are (a) build the Composer AI Assist feature for real (BYOK, no new
GSI-held secret needed), or (b) build and deploy an Intelligence backend
service (a much larger, separate product effort). See
`~/lane-reports/brightbean-ai-not-operating.md` for the full audit and
recommended next steps.
