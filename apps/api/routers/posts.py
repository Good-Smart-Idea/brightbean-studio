"""``/api/v1/posts/*`` — compose, review, approve internally, schedule, cancel.

Every write path:

1. Enforces HTTP-level rate limits (per-key, per-workspace, global).
2. Replays the response if an idempotency key matches an earlier request.
3. Checks workspace permission via the shared ``@require_permission`` helper.
4. Validates the target ``SocialAccount`` is in the key's allowlist.
5. Checks per-platform 24h quota.
6. Calls the existing composer/approval service layer for state transitions.
7. Writes an audit log row.
8. Persists the response under the idempotency key if one was passed.

This ordering means an expensive operation (the create) can't run until
both the cheap rate-limit check and the issuer-permission check pass —
defence against spending DB cycles for unauthorized callers.
"""

from __future__ import annotations

import uuid
from typing import Literal

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from apps.api.limits import check_platform_quota, enforce_http_rate_limits
from apps.api.middleware import (
    claim_idempotency_slot,
    finalize_idempotent_response,
    fingerprint_request,
    log_audit_entry,
    release_idempotent_claim,
)
from apps.api.schemas import (
    CreatePostRequest,
    PostResponse,
    ScheduleRequest,
    UpdatePostRequest,
)
from apps.composer.models import Post
from apps.composer.services import (
    create_post,
    media_change_blocker,
    sync_post_scheduled_at,
    transition_platform_post,
    usable_media_types,
)
from apps.social_accounts.models import SocialAccount

router = Router(tags=["posts"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_perm(request: HttpRequest, key: str) -> None:
    """Re-check a workspace permission inside a Ninja route body.

    We can't decorate Ninja routes with ``@require_permission`` because it
    expects a Django view signature; instead we inline the same check
    against the virtual membership shim.
    """
    membership = getattr(request, "workspace_membership", None)
    if membership is None or not membership.effective_permissions.get(key, False):
        raise HttpError(403, f"Permission denied: {key}")


def _resolve_account(request: HttpRequest, social_account_id: uuid.UUID) -> SocialAccount:
    """Resolve the target account and verify it is in the key's allowlist.

    Defence against confused-deputy attacks: even though the bearer is
    valid, the caller may only act on accounts the issuer explicitly
    listed at issuance time.
    """
    api_key = request.api_key  # type: ignore[attr-defined]  # set by ApiKeyAuth
    allowlist_ids = {sa.id for sa in api_key.social_accounts.all()}
    if social_account_id not in allowlist_ids:
        raise HttpError(403, "SocialAccount is not in this key's allowlist.")
    return SocialAccount.objects.get(id=social_account_id)


def _can_view_internal_notes(request: HttpRequest) -> bool:
    """Whether this caller may see a post's team-only ``internal_notes``.

    The composer hides ``internal_notes`` from the ``client`` / ``viewer``
    workspace roles ([views.py](apps/composer/views.py): ``can_view_internal_notes``).
    Those are exactly the builtin roles without ``create_posts`` (see
    ``BUILTIN_ROLE_PERMISSIONS``), so gating visibility on ``create_posts``
    reproduces that rule uniformly for both API keys and OAuth members. Reads
    (`GET /posts/{id}`) aren't otherwise permission-gated, so without this the
    shared serializer would leak notes to any read-capable client/viewer.
    """
    membership = getattr(request, "workspace_membership", None)
    return bool(membership and membership.effective_permissions.get("create_posts", False))


def _post_to_response(request: HttpRequest, post: Post) -> PostResponse:
    return PostResponse.from_post(post, include_internal_notes=_can_view_internal_notes(request))


def _get_workspace_post(request: HttpRequest, post_id: uuid.UUID) -> Post:
    """Fetch a Post that belongs to the key's workspace **and** whose every
    ``PlatformPost`` child targets a ``SocialAccount`` in the key's
    allowlist.

    The workspace filter alone is not enough — a key scoped to LinkedIn-A
    could otherwise read or mutate a Post whose only child is for
    Twitter-B in the same workspace if it happened to know the Post UUID.
    The "all children in allowlist" rule (rather than "any child") means
    schedule/cancel/update can freely iterate ``post.platform_posts``
    without us having to scope each operation sub-Post — there is no
    foreign child for them to touch.

    We intentionally don't distinguish "doesn't exist" from "exists in
    another workspace" from "exists but partially out of scope" — all
    three return 404 so the API doesn't leak the existence of foreign
    IDs to a partial-scope bearer.
    """
    from django.http import Http404

    post = get_object_or_404(
        Post.objects.prefetch_related("platform_posts__social_account"),
        id=post_id,
        workspace_id=request.api_key.workspace_id,  # type: ignore[attr-defined]
    )
    allowed_ids = {sa.id for sa in request.api_key.social_accounts.all()}  # type: ignore[attr-defined]
    pp_account_ids = {pp.social_account_id for pp in post.platform_posts.all()}
    # No platform_posts → nothing this key could legitimately act on.
    # Foreign child → leaking even via a read would be a confused-deputy.
    if not pp_account_ids or not pp_account_ids.issubset(allowed_ids):
        raise Http404()
    return post


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/", response={201: PostResponse, 200: PostResponse}, summary="Create a draft or scheduled post")
def create(request, payload: CreatePostRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    # Scheduling implies "ready to publish without further human review",
    # which is exactly what ``publish_directly`` gates in the composer
    # ([views.py:797](apps/composer/views.py)). The REST API must mirror
    # that contract — a key issued with only ``create_posts`` can park
    # drafts but cannot send them into the publisher's poll loop.
    if payload.action == "schedule":
        _require_perm(request, "publish_directly")

    # ---- Cheap validation runs BEFORE we touch the idempotency table.
    # The motivation: an early failure here (403, 422) would otherwise
    # leave a placeholder claim row that release-on-error has to clean
    # up — and forgetting one release path locks the agent out for 24h.
    # Doing all "can this request possibly succeed?" checks pre-claim
    # keeps the claim/release pair tight and confined to the post-claim
    # path (platform quota + create_post).
    social_account = _resolve_account(request, payload.social_account_id)
    if payload.action == "schedule" and payload.scheduled_at is None:
        raise HttpError(422, "scheduled_at is required when action='schedule'.")

    # Build the platform_overrides dict and validate that each override's
    # social_account_id matches one of the post's target accounts. In the
    # current single-account API that's only ``payload.social_account_id``;
    # anything else would silently no-op at publish time, so we reject up
    # front. The plan's [Gap 2] section calls this out explicitly.
    platform_overrides: dict = {}
    for ov in payload.platform_overrides:
        if ov.social_account_id != payload.social_account_id:
            raise HttpError(
                422,
                (
                    f"platform_overrides[*].social_account_id must match the post's "
                    f"social_account_id ({payload.social_account_id}); got {ov.social_account_id}."
                ),
            )
        platform_overrides[ov.social_account_id] = {
            "title": ov.title,
            "caption": ov.caption,
            "first_comment": ov.first_comment,
        }

    # ---- Atomic claim-first idempotency. Three early-out branches
    # before we do *any* mutating work, so concurrent identical retries
    # can never both reach create_post:
    #   replay     — prior request already finished, return its response
    #   in_flight  — a concurrent peer holds the slot, return 409
    #   passthrough/claimed — caller proceeds; only the "claimed" path
    #                         must finalize or release before returning.
    fingerprint = fingerprint_request(request.method or "POST", request.path, payload.dict(by_alias=True))
    # Accept the canonical ``Idempotency-Key`` HTTP header (Stripe-style)
    # in addition to the body field. The header is the industry
    # convention — clients that retry on network timeout typically reuse
    # the header but can't easily re-send the same JSON body — so
    # ignoring it would silently break the "retry-safe" contract Codex
    # review (PR #53) flagged. The body field still wins when both are
    # present, so a deliberate caller can override.
    idempotency_key = payload.idempotency_key or request.headers.get("Idempotency-Key") or None
    try:
        disposition, replay_status, replay_body = claim_idempotency_slot(
            api_key=request.api_key,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    if disposition == "replay":
        return replay_status, replay_body
    if disposition == "in_flight":
        raise HttpError(
            409,
            "An identical request with this idempotency_key is still in flight; retry shortly.",
        )

    # Platform-quota check has to be inside the claim window because it
    # depends on database state that other concurrent claims could
    # change. Drafts are excluded from the count inside
    # ``check_platform_quota`` so creating drafts cannot exhaust the
    # platform's posting cap.
    if payload.action == "schedule":
        try:
            check_platform_quota(social_account)
        except HttpError:
            release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
            raise

    # Single try/except covers every step from create_post through
    # finalize_idempotent_response. Codex review found that earlier
    # code committed the Post + PlatformPost via create_post and then
    # built the response / wrote the audit / called finalize OUTSIDE
    # the release path — so a transient DB error during response build
    # or finalize left the idempotency slot wedged in PENDING forever
    # while the work had already succeeded. Folding everything under
    # one ``try / except / release`` closes that window: any exception
    # after the claim releases the slot, the agent can retry, and the
    # retry will reach create_post fresh (or replay if finalize did
    # commit before the failure).
    try:
        post = create_post(
            workspace=request.api_key.workspace,
            social_account=social_account,
            caption=payload.caption,
            media_asset_ids=payload.media_asset_ids,
            title=payload.title,
            first_comment=payload.first_comment,
            internal_notes=payload.internal_notes,
            scheduled_at=payload.scheduled_at,
            # A scheduled post carries a real time, not a proposal — ignore any
            # proposed_publish_at when scheduling so the two never coexist.
            proposed_publish_at=None if payload.action == "schedule" else payload.proposed_publish_at,
            author=request.user if not request.user.is_anonymous else None,
            status="scheduled" if payload.action == "schedule" else "draft",
            platform_overrides=platform_overrides,
        )
        body = _post_to_response(request, post)
        status_code = 201
        log_audit_entry(
            request,
            action=f"post.create.{payload.action}",
            target_id=post.id,
            status_code=status_code,
        )
        # ``model_dump(mode='json')`` yields JSON-safe primitives
        # (UUID→str, datetime→ISO-8601 str) so the response_body
        # JSONField round-trips cleanly through psycopg's jsonb adapter.
        finalize_idempotent_response(
            api_key=request.api_key,
            idempotency_key=idempotency_key,
            status_code=status_code,
            body=body.model_dump(mode="json"),
        )
    except ValueError as exc:
        # Use the *effective* idempotency key (header fallback applied)
        # so a header-only client's claim is released too — Codex PR #53
        # round-3 flagged that the previous ``payload.idempotency_key``
        # was None on header-only requests and left the claim wedged in
        # PENDING until the 24h sweep.
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise HttpError(422, str(exc)) from exc
    except Exception:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise
    return status_code, body


@router.get("/{post_id}", response=PostResponse, summary="Read a single post")
def retrieve(request, post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    post = _get_workspace_post(request, post_id)
    log_audit_entry(request, action="post.read", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


class FacebookVideoFormatRequest(Schema):
    publish_as: Literal["video", "reel"]

    class Config:
        extra = "forbid"


class FacebookVideoFormatResponse(Schema):
    post_id: uuid.UUID
    platform_post_id: uuid.UUID
    social_account_id: uuid.UUID
    status: str
    configured_publish_as: Literal["video", "reel"]
    resolved_post_type: str


def _facebook_video_format_response(post, child) -> FacebookVideoFormatResponse:
    from apps.publisher.engine import PublishEngine

    media_types = list(post.media_attachments.values_list("media_asset__media_type", flat=True))
    extras = child.platform_extra or {}
    resolved = PublishEngine._resolve_post_type(
        "facebook", extras, len(media_types), media_types[0] if media_types else None
    )
    return FacebookVideoFormatResponse(
        post_id=post.id,
        platform_post_id=child.id,
        social_account_id=child.social_account_id,
        status=child.status,
        configured_publish_as="reel" if extras.get("post_type") == "reel" else "video",
        resolved_post_type=resolved.value,
    )


_FACEBOOK_VIDEO_FORMAT_PATH = "/{post_id}/platform-posts/{platform_post_id}/facebook-video-format"


@router.get(_FACEBOOK_VIDEO_FORMAT_PATH, response=FacebookVideoFormatResponse, summary="Read Facebook video format")
def read_facebook_video_format(request, post_id: uuid.UUID, platform_post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    post = _get_workspace_post(request, post_id)
    child = get_object_or_404(
        post.platform_posts,
        id=platform_post_id,
        social_account__platform="facebook",
        social_account__workspace=post.workspace,
    )
    result = _facebook_video_format_response(post, child)
    log_audit_entry(request, action="post.facebook_video_format.read", target_id=child.id, status_code=200)
    return result


@router.patch(
    _FACEBOOK_VIDEO_FORMAT_PATH, response=FacebookVideoFormatResponse, summary="Set a draft Facebook video format"
)
def update_facebook_video_format(
    request, post_id: uuid.UUID, platform_post_id: uuid.UUID, payload: FacebookVideoFormatRequest
):
    """Edit one unsent draft child, without withdrawing approval or publishing.

    Other platform children, including published siblings, remain unchanged.
    Generic platform_extra values are neither accepted nor returned.
    """
    from django.db import transaction
    from django.http import Http404

    from apps.composer.models import PlatformPost, PostMedia
    from apps.media_library.models import MediaAsset
    from providers.facebook import FACEBOOK_REEL_MAX_DURATION_SEC, FACEBOOK_REEL_MIN_DURATION_SEC

    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    scoped_post = _get_workspace_post(request, post_id)
    with transaction.atomic():
        post = Post.objects.select_for_update().get(id=scoped_post.id)
        if post.workspace_id != request.api_key.workspace_id:
            raise Http404()
        if post.author_id != request.user.id:
            _require_perm(request, "edit_others_posts")
        children = list(PlatformPost.objects.select_for_update().select_related("social_account").filter(post=post))
        allowed_ids = set(request.api_key.social_accounts.values_list("id", flat=True))
        if any(child.social_account_id not in allowed_ids for child in children):
            raise Http404()
        child = next((child for child in children if child.id == platform_post_id), None)
        if (
            child is None
            or child.social_account.platform != "facebook"
            or child.social_account.workspace_id != post.workspace_id
        ):
            raise Http404()
        if child.status != "draft" or any(
            (child.platform_post_id, child.published_at, child.scheduled_at, child.next_retry_at, child.retry_count)
        ):
            raise HttpError(409, "Only an unsent draft Facebook child can change video format.")

        attachments = list(PostMedia.objects.select_for_update().filter(post=post))
        if len(attachments) != 1:
            raise HttpError(422, "Facebook video format requires exactly one completed video.")
        asset = MediaAsset.objects.select_for_update().get(id=attachments[0].media_asset_id)
        if not (
            asset.workspace_id == post.workspace_id
            or (asset.workspace_id is None and asset.organization_id == post.workspace.organization_id)
        ):
            raise Http404()
        if asset.media_type != "video" or asset.processing_status != "completed" or not asset.file:
            raise HttpError(422, "Facebook video format requires exactly one completed video.")
        if (
            payload.publish_as == "reel"
            and asset.duration
            and not (FACEBOOK_REEL_MIN_DURATION_SEC <= asset.duration <= FACEBOOK_REEL_MAX_DURATION_SEC)
        ):
            raise HttpError(422, "Facebook Reels must be between 3 and 90 seconds when duration is known.")

        extra = dict(child.platform_extra or {})
        if payload.publish_as == "reel":
            extra["post_type"] = "reel"
        else:
            extra.pop("post_type", None)
        if child.platform_extra != extra:
            child.platform_extra = extra
            child.save(update_fields=["platform_extra", "updated_at"])
        result = _facebook_video_format_response(post, child)
        log_audit_entry(request, action="post.facebook_video_format.update", target_id=child.id, status_code=200)
    return result


@router.patch("/{post_id}", response=PostResponse, summary="Update draft fields")
def update(request, post_id: uuid.UUID, payload: UpdatePostRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")  # mirrors composer's create-or-edit perm
    post = _get_workspace_post(request, post_id)

    # Only allow updates while editable.
    if not post.is_editable:
        raise HttpError(409, f"Post is not editable in status {post.status}.")

    # Re-timing a scheduled post is publish-budget behaviour: pushing
    # ``scheduled_at`` into the past makes the publisher fire the post on
    # its very next poll (~15 s), and pushing it far into the future buries
    # admin-scheduled content. Either is a privilege escalation for a key
    # that doesn't hold ``publish_directly`` — the create / schedule routes
    # and every MCP transition tool gate this exact mutation on that
    # permission, and the PATCH route must do the same to stay consistent.
    # Codex PR #53 security review (round 4) caught this gap.
    if payload.scheduled_at is not None and post.platform_posts.filter(status="scheduled").exists():
        _require_perm(request, "publish_directly")

    # ---- Validate-everything-first.
    #
    # Codex review found two bugs in the previous implementation:
    #   (a) ``scheduled_children.update(scheduled_at=...)`` ran BEFORE
    #       media validation, so a 422 from a foreign media asset still
    #       committed the new schedule timestamp to the DB.
    #   (b) ``post.media_attachments.all().delete()`` + the create loop
    #       were not wrapped in a transaction, so a mid-loop failure
    #       deleted the originals and persisted only a partial new set.
    # Fix: resolve every required reference and reject every invalid
    # input before any database mutation, then perform all mutations
    # inside a single ``transaction.atomic()`` block. Either everything
    # commits or nothing does.
    from django.db import transaction

    from apps.composer.models import PostMedia
    from apps.media_library.models import MediaAsset

    wanted_media: list = []
    resolved_assets: dict = {}
    if payload.media_asset_ids is not None:
        wanted_media = list(payload.media_asset_ids)
        resolved_assets = {a.id: a for a in MediaAsset.objects.filter(id__in=wanted_media, workspace=post.workspace)}
        missing = [i for i in wanted_media if i not in resolved_assets]
        if missing:
            raise HttpError(422, f"Media asset(s) not in workspace: {missing}")
        # A scheduled post may be edited, but not into one it can't publish:
        # swapping a TikTok post's video for an image, or emptying a pin's
        # media, would leave it queued to fail after the schedule route had
        # checked it.
        reason = media_change_blocker(
            post, media_types_after=usable_media_types(resolved_assets[mid] for mid in wanted_media)
        )
        if reason:
            raise HttpError(422, reason)

    with transaction.atomic():
        update_fields: list[str] = []
        if payload.caption is not None:
            post.caption = payload.caption
            update_fields.append("caption")
        if payload.title is not None:
            post.title = payload.title
            update_fields.append("title")
        if payload.first_comment is not None:
            post.first_comment = payload.first_comment
            update_fields.append("first_comment")
        if payload.internal_notes is not None:
            post.internal_notes = payload.internal_notes
            update_fields.append("internal_notes")
        if payload.scheduled_at is not None:
            # Re-time any currently-scheduled child. Drafts are unaffected.
            # ``QuerySet.update()`` bypasses ``auto_now``, so we include
            # ``updated_at=timezone.now()`` explicitly. Otherwise each
            # child's ``updated_at`` would freeze at its creation time
            # despite an effective state change.
            from django.utils import timezone as _tz

            scheduled_children = post.platform_posts.filter(status="scheduled")
            scheduled_children.update(scheduled_at=payload.scheduled_at, updated_at=_tz.now())
            post.scheduled_at = payload.scheduled_at
            update_fields.append("scheduled_at")
        if payload.proposed_publish_at is not None:
            # Draft-stage suggestion; null is a no-op, consistent with the
            # other PATCH fields. Not gated on publish_directly — it never
            # reaches the publisher.
            post.proposed_publish_at = payload.proposed_publish_at
            update_fields.append("proposed_publish_at")
        if payload.media_asset_ids is not None:
            # Replace the attachment set in order. Validated above, so
            # the only remaining failure modes are DB-level — the atomic
            # block rolls back the whole route if any single
            # ``PostMedia.objects.create`` raises.
            post.media_attachments.all().delete()
            for position, mid in enumerate(wanted_media):
                PostMedia.objects.create(
                    post=post,
                    media_asset=resolved_assets[mid],
                    position=position,
                )

        if update_fields:
            post.save(update_fields=[*update_fields, "updated_at"])

        sync_post_scheduled_at(post)

    post.refresh_from_db()
    log_audit_entry(request, action="post.update", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.post("/{post_id}/submit-for-review", response=PostResponse, summary="Submit a draft for internal approval")
def submit_for_review(request, post_id: uuid.UUID):
    """Request human review without scheduling, approving, or publishing.

    Pending-review retries are a no-op. Lock the post and its children so
    concurrent submissions cannot create duplicate approval actions.
    """
    from django.db import transaction

    from apps.approvals.services import submit_for_review as submit_post_for_review
    from apps.composer.models import PlatformPost

    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    scoped_post = _get_workspace_post(request, post_id)
    if scoped_post.author_id != request.user.id:
        _require_perm(request, "edit_others_posts")

    with transaction.atomic():
        post = Post.objects.select_for_update().get(id=scoped_post.id)
        children = list(PlatformPost.objects.select_for_update().filter(post=post))
        states = {pp.status for pp in children}
        if not states or not states.issubset({"draft", "pending_review"}):
            raise HttpError(409, "Only draft or already pending-review posts can be submitted.")
        if "draft" in states:
            submit_post_for_review(post, request.user, post.workspace)

    post.refresh_from_db()
    log_audit_entry(request, action="post.submit_for_review", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.post("/{post_id}/return-to-draft", response=PostResponse, summary="Withdraw a pending review to draft")
def return_to_draft(request, post_id: uuid.UUID):
    """Withdraw an internal/client review without touching publish states.

    Draft retries are no-ops. All children must be drafts or awaiting review;
    a scheduled, approved, published, or otherwise non-review child rejects the
    whole operation before any transition or audit write.
    """
    from django.db import transaction

    from apps.approvals.services import withdraw_review
    from apps.composer.models import PlatformPost

    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    scoped_post = _get_workspace_post(request, post_id)
    if scoped_post.author_id != request.user.id:
        _require_perm(request, "edit_others_posts")

    with transaction.atomic():
        post = Post.objects.select_for_update().get(id=scoped_post.id)
        children = list(PlatformPost.objects.select_for_update().filter(post=post))
        # Recheck account scope after locking: a concurrent editor may have
        # added a target since the initial scoped lookup.
        allowed_ids = set(request.api_key.social_accounts.values_list("id", flat=True))
        if any(pp.social_account_id not in allowed_ids for pp in children):
            from django.http import Http404

            raise Http404()
        states = {pp.status for pp in children}
        if not states or not states.issubset({"draft", "pending_review", "pending_client"}):
            raise HttpError(409, "Only drafts or pending-review posts can be returned to draft.")
        withdraw_review(post, request.user, post.workspace)
        sync_post_scheduled_at(post)

    post.refresh_from_db()
    log_audit_entry(request, action="post.return_to_draft", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


class ApprovePostRequest(Schema):
    reason: str = Field(..., min_length=1, max_length=2000)

    class Config:
        extra = "forbid"


def _locked_approval_actor(request):
    """Fresh explicit-key/live-member intersection, held until decision commit.

    Lock order: key, actual user, membership, custom role, workspace, post,
    children, accounts, allowlist rows. Parent FK locks also serialize child
    and allowlist insertion; through-row locks serialize scope deletion.
    """
    from apps.accounts.models import User
    from apps.api_keys.models import ApiKey
    from apps.members.models import CustomRole, WorkspaceMembership

    authenticated = request.api_key
    key = ApiKey.objects.select_for_update().filter(id=authenticated.id).first()
    if (
        key is None
        or not key.is_active
        or key.issued_by_id != request.user.id
        or key.workspace_id != authenticated.workspace_id
        or key.token_hash != authenticated.token_hash
    ):
        raise HttpError(403, "The API approval actor is no longer authorized.")
    actor = User.objects.select_for_update().filter(id=key.issued_by_id, is_active=True).first()
    membership = (
        WorkspaceMembership.objects.select_for_update()
        .filter(user_id=key.issued_by_id, workspace_id=key.workspace_id)
        .first()
    )
    if actor is None or membership is None:
        raise HttpError(403, "The API approval actor is no longer authorized.")
    if membership.custom_role_id is not None:
        membership.custom_role = CustomRole.objects.select_for_update().get(id=membership.custom_role_id)
    effective = set(key.permissions or []) & {
        permission for permission, allowed in membership.effective_permissions.items() if allowed
    }
    if not {"create_posts", "approve_posts"}.issubset(effective):
        raise HttpError(403, "Permission denied: create_posts and approve_posts are required.")
    return key, actor, effective


def _has_approval_publish_evidence(child):
    """Approval never adopts or resets evidence of a prior publish attempt."""
    return any(
        (
            child.platform_post_id,
            child.published_at,
            child.scheduled_at,
            child.retry_count,
            child.next_retry_at,
            child.publish_error,
            child.native_upload_state,
            child.first_comment_status != "none",
            child.first_comment_id,
            child.first_comment_error,
            child.first_comment_retry_count,
            child.first_comment_posted_at,
        )
    )


@router.post("/{post_id}/approve", response=PostResponse, summary="Approve a submitted internal review")
def approve(request, post_id: uuid.UUID, payload: ApprovePostRequest):
    """An audited API-issuer decision; never approve a client stage or publish.

    The existing approval service owns transitions, mandatory ApprovalAction,
    and normal two-stage client reminders/notifications. All-approved retries
    are no-ops; a two-stage internal decision returns pending_client honestly.
    """
    from django.db import transaction
    from django.http import Http404

    from apps.approvals.services import approve_post
    from apps.composer.models import PlatformPost
    from apps.publisher.models import PublishLog
    from apps.workspaces.models import Workspace

    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    _require_perm(request, "approve_posts")
    reason = payload.reason.strip()
    if not reason:
        raise HttpError(422, "An approval reason is required.")
    scoped_post = _get_workspace_post(request, post_id)
    with transaction.atomic():
        key, actor, effective = _locked_approval_actor(request)
        workspace = Workspace.objects.select_for_update().get(id=key.workspace_id)
        post = get_object_or_404(Post.objects.select_for_update(), id=scoped_post.id)
        if post.workspace_id != workspace.id:
            raise Http404()
        if post.author_id != actor.id and "edit_others_posts" not in effective:
            raise HttpError(403, "Permission denied: edit_others_posts")
        children = list(PlatformPost.objects.select_for_update().filter(post=post).order_by("id"))
        account_ids = {child.social_account_id for child in children}
        accounts = list(SocialAccount.objects.select_for_update().filter(id__in=account_ids).order_by("id"))
        # Do not use the auth object's potentially cached/prefetched allowlist.
        through = key.social_accounts.through
        allowed_ids = set(
            through.objects.select_for_update().filter(apikey_id=key.id).values_list("socialaccount_id", flat=True)
        )
        if (
            not children
            or not account_ids.issubset(allowed_ids)
            or len(accounts) != len(account_ids)
            or any(account.workspace_id != workspace.id for account in accounts)
        ):
            raise Http404()
        if (
            post.scheduled_at is not None
            or post.published_at is not None
            # PublishLog is the append-only attempt ledger (including Meta
            # STARTED before provider I/O), not merely an HTTP response cache.
            # Any retained row contradicts this operation's strict unsent
            # contract. Never interpret malformed/unmatched history as absent.
            # Locked child FK parents serialize new ledger-row commits.
            or PublishLog.objects.filter(platform_post_id__in=[child.id for child in children]).exists()
            or any(
                child.status not in {"pending_review", "approved"} or _has_approval_publish_evidence(child)
                for child in children
            )
        ):
            raise HttpError(409, "Only unsent pending-review or already-approved targets can be approved internally.")
        moved = any(child.status == "pending_review" for child in children)
        if moved:
            approve_post(post, actor, workspace, reason)
        # Attribute the request audit to the same freshly authorized real key.
        request.api_key = key
        request.user = actor
    post.refresh_from_db()
    log_audit_entry(
        request, action="post.approve" if moved else "post.approve.noop", target_id=post.id, status_code=200
    )
    return _post_to_response(request, post)


class WithdrawApprovalRequest(Schema):
    reason: str = Field(..., min_length=1, max_length=2000)


@router.post("/{post_id}/withdraw-approval", response=PostResponse, summary="Withdraw an unsent approval to draft")
def withdraw_approval(request, post_id: uuid.UUID, payload: WithdrawApprovalRequest):
    """An explicit, audited approval decision; never schedules or unpublishes."""
    from django.db import transaction
    from django.http import Http404

    from apps.approvals.services import withdraw_approval as withdraw_post_approval
    from apps.composer.models import PlatformPost

    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    _require_perm(request, "approve_posts")
    if not payload.reason.strip():
        raise HttpError(422, "A withdrawal reason is required.")
    scoped_post = _get_workspace_post(request, post_id)
    with transaction.atomic():
        post = Post.objects.select_for_update().get(id=scoped_post.id)
        if post.workspace_id != request.api_key.workspace_id:
            raise Http404()
        if post.author_id != request.user.id:
            _require_perm(request, "edit_others_posts")
        children = list(PlatformPost.objects.select_for_update().filter(post=post))
        allowed_ids = set(request.api_key.social_accounts.values_list("id", flat=True))
        if any(pp.social_account_id not in allowed_ids for pp in children):
            raise Http404()
        try:
            withdraw_post_approval(post, request.user, post.workspace, payload.reason)
        except ValueError as exc:
            raise HttpError(409, str(exc)) from exc
        log_audit_entry(request, action="post.withdraw_approval", target_id=post.id, status_code=200)
    post.refresh_from_db()
    return _post_to_response(request, post)


@router.post("/{post_id}/schedule", response=PostResponse, summary="Schedule a draft or final-approved post")
def schedule(request, post_id: uuid.UUID, payload: ScheduleRequest):
    enforce_http_rate_limits(request, is_write=True)
    # Same ``publish_directly`` contract as the create-with-schedule
    # branch: only keys that can publish-directly may push a post into
    # the SCHEDULED state. See ``create``.
    _require_perm(request, "create_posts")
    _require_perm(request, "publish_directly")
    post = _get_workspace_post(request, post_id)

    if post.author_id != request.user.id:
        _require_perm(request, "edit_others_posts")

    from django.db import transaction
    from django.http import Http404

    from apps.composer.models import PlatformPost

    with transaction.atomic():
        post = Post.objects.select_for_update().get(id=post.id)
        if post.author_id != request.user.id:
            _require_perm(request, "edit_others_posts")
        children = list(PlatformPost.objects.select_for_update().filter(post=post))
        allowed_ids = set(request.api_key.social_accounts.values_list("id", flat=True))
        if any(pp.social_account_id not in allowed_ids for pp in children):
            raise Http404()
        retry = bool(children) and all(
            pp.status == "scheduled" and pp.scheduled_at == payload.scheduled_at for pp in children
        )
        if not retry:
            if not children or any(pp.status not in {"draft", "approved"} for pp in children):
                raise HttpError(409, "No draft or final-approved platform posts to schedule atomically.")
            for pp in children:
                check_platform_quota(pp.social_account)
            for pp in children:
                try:
                    transition_platform_post(pp, "scheduled", scheduled_at=payload.scheduled_at)
                except ValueError as exc:
                    raise HttpError(422, str(exc)) from exc

    post.refresh_from_db()
    log_audit_entry(request, action="post.schedule", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.post("/{post_id}/cancel", response=PostResponse, summary="Cancel a scheduled post (back to draft)")
def cancel(request, post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    post = _get_workspace_post(request, post_id)

    scheduled_children = list(post.platform_posts.filter(status="scheduled"))
    if not scheduled_children:
        raise HttpError(409, "No scheduled platform posts to cancel.")

    # Same atomic-loop reasoning as ``schedule``: any per-child transition
    # failure rolls back the whole cancellation. Half-cancelled posts
    # would otherwise leave the publisher with a mix of ``draft`` and
    # ``scheduled`` children, which is exactly the inconsistent state
    # the route was supposed to prevent.
    from django.db import transaction

    with transaction.atomic():
        for pp in scheduled_children:
            try:
                transition_platform_post(pp, "draft")
            except ValueError as exc:
                raise HttpError(422, str(exc)) from exc

    post.refresh_from_db()
    log_audit_entry(request, action="post.cancel", target_id=post.id, status_code=200)
    return _post_to_response(request, post)
