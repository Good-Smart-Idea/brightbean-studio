"""List scoped accounts and read their native published media without mutating posts."""

from __future__ import annotations

import uuid

from ninja import Query, Router, Schema
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.recent_media import (
    NativeReceiptFailure,
    NativeReceiptLookupError,
    NativeVideoResponse,
    RecentMediaResponse,
    fetch_facebook_video,
    fetch_recent_media,
    valid_cursor,
)
from apps.api.routers.posts import _require_perm, _resolve_account
from apps.api.schemas import AccountsListResponse, AccountSummary

router = Router(tags=["accounts"])


@router.get(
    "/",
    response=AccountsListResponse,
    summary="List the SocialAccounts this API key is allowed to act on",
)
def list_accounts(request):
    enforce_http_rate_limits(request, is_write=False)
    api_key = request.api_key
    accounts = [AccountSummary.from_social_account(sa) for sa in api_key.social_accounts.all()]
    log_audit_entry(request, action="accounts.list", target_id=None, status_code=200)
    return AccountsListResponse(accounts=accounts)


@router.get(
    "/{account_id}/recent-media",
    response=RecentMediaResponse,
    summary="Read one native Instagram media or Facebook video page for reconciliation",
    description=(
        "Read-only: never publishes, retries, refreshes credentials, or changes post state. "
        "Requires view_analytics and the account allowlist. A page proves presence, not absence: "
        "follow next_cursor until the intended time window is covered. No arbitrary provider URLs are accepted."
    ),
)
def recent_media(
    request,
    account_id: uuid.UUID,
    limit: int = Query(25, ge=1, le=50),  # noqa: B008 — Ninja query validation
    after: str | None = None,
):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    account = _resolve_account(request, account_id)
    if account.platform not in ("instagram", "instagram_login", "facebook"):
        raise HttpError(
            422,
            "Native recent media is supported only for Instagram and Facebook accounts.",
        )
    if not valid_cursor(after):
        raise HttpError(422, "Invalid pagination cursor.")
    try:
        result = fetch_recent_media(account, limit=limit, after=after)
    except Exception:
        # Provider exceptions may contain request URLs/tokens or app details.
        # Never send or log those raw details through the agent surface.
        raise HttpError(502, "Native media could not be read. No post was changed or retried.") from None
    log_audit_entry(request, action="accounts.recent_media", target_id=account.id, status_code=200)
    return result


@router.get(
    "/{account_id}/published-media/{platform_post_id}",
    response={200: NativeVideoResponse, 502: NativeReceiptFailure},
    summary="Read native Facebook video receipt and destination without retrying",
)
def published_media(request, account_id: uuid.UUID, platform_post_id: uuid.UUID):
    from django.shortcuts import get_object_or_404

    from apps.composer.models import PlatformPost

    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    account = _resolve_account(request, account_id)
    if account.platform != "facebook":
        raise HttpError(422, "Exact native video reconciliation is supported only for Facebook.")
    # No arbitrary provider IDs: only a receipt already belonging to this exact
    # allowlisted account/workspace can be inspected with its credential.
    platform_post = get_object_or_404(
        PlatformPost,
        id=platform_post_id,
        social_account_id=account.id,
        post__workspace_id=request.api_key.workspace_id,
    )
    try:
        result = fetch_facebook_video(account, platform_post)
    except NativeReceiptLookupError as exc:
        return 502, exc.diagnostic
    except Exception:
        raise HttpError(502, "Native video could not be verified. No post was changed or retried.") from None
    log_audit_entry(
        request,
        action="accounts.published_media",
        target_id=account.id,
        status_code=200,
    )
    return result


class NativeVideoConfigurationResponse(Schema):
    supported: bool
    configured: bool
    blocker: str
    provider_eligibility_verified: bool = False


@router.get(
    "/{account_id}/native-video-readiness",
    response=NativeVideoConfigurationResponse,
    summary="Read scoped native-video credential configuration without contacting Meta",
    description=(
        "Configuration preflight only: no token values, provider requests, refreshes, "
        "grants, scheduling, or publication. A configured grant is not verified provider eligibility."
    ),
)
def native_video_readiness(request, account_id: uuid.UUID):
    from django.http import Http404

    from apps.social_accounts.native_video import native_video_configuration

    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "create_posts")
    _require_perm(request, "publish_directly")
    account = _resolve_account(request, account_id)
    if account.workspace_id != request.api_key.workspace_id:
        raise Http404()
    result = native_video_configuration(account)
    log_audit_entry(
        request,
        action="accounts.native_video_configuration",
        target_id=account.id,
        status_code=200,
    )
    return result
