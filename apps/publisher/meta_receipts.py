"""Internal append-only Meta attempt provenance, separate from upload progress.

STARTED has no HTTP status: it is committed before any provider invocation.
ACCEPTED records a returned receipt; RECOVERED records durable-cache recovery
without pretending another HTTP request occurred. None are client API input.
"""

import hashlib
import json
import uuid

from django.db import transaction

from apps.composer.models import PlatformPost
from apps.publisher.models import PublishLog
from providers.exceptions import PublishError
from providers.meta_confirmation import native_id

STARTED = "meta_publish_started"
ACCEPTED = "meta_publish_accepted"
RECOVERED = "meta_publish_recovered"


def binding(platform_post, *, grant_kind, content_type, attachments=None):
    account = platform_post.social_account
    token = account.oauth_facebook_user_access_token if grant_kind == "facebook_user" else account.oauth_access_token
    extras = {
        k: v
        for k, v in (platform_post.platform_extra or {}).items()
        if k not in {"id", "video_id", "post_id", "permalink_url"}
    }
    if attachments is None:
        media = list(
            platform_post.post.media_attachments.order_by("position", "pk").values_list(
                "media_asset_id",
                "media_asset__file",
                "media_asset__workspace_id",
                "media_asset__organization_id",
                "media_asset__media_type",
            )
        )
    else:
        media = [
            (
                pm.media_asset_id,
                pm.media_asset.file.name,
                pm.media_asset.workspace_id,
                pm.media_asset.organization_id,
                pm.media_asset.media_type,
            )
            for pm in attachments
        ]
    inputs = [
        account.pk,
        account.platform,
        account.account_platform_id,
        account.workspace_id,
        platform_post.post.workspace_id,
        platform_post.post.author_id,
        hashlib.sha256(str(token or "").encode()).hexdigest(),
        grant_kind,
        content_type,
        platform_post.effective_caption,
        platform_post.post.tags,
        extras,
        platform_post.platform_specific_media,
        media,
    ]
    return hashlib.sha256(json.dumps(inputs, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _entries(row):
    entries = []
    for log in row.publish_logs.order_by("-created_at")[:30]:
        try:
            value = json.loads(log.response_body)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("kind") in {STARTED, ACCEPTED, RECOVERED}:
            entries.append(value)
    return entries


def _scoped(marker, row):
    account = row.social_account
    return (
        marker.get("child_id") == str(row.pk)
        and marker.get("account_id") == str(account.pk)
        and marker.get("workspace_id") == str(row.post.workspace_id)
        and marker.get("owner_id") == account.account_platform_id
        and marker.get("platform") == account.platform
        and marker.get("grant_kind")
        in ({"account", "facebook_user"} if account.platform == "instagram" else {"account"})
        and account.workspace_id == row.post.workspace_id
    )


def accepted_attempt(row):
    """Return only a matching internal STARTED→ACCEPTED provenance pair."""
    entries = _entries(row)
    accepted = next((x for x in entries if x.get("kind") in {ACCEPTED, RECOVERED}), None)
    if not accepted or not _scoped(accepted, row) or not native_id(accepted.get("receipt_id")):
        return None
    started = next(
        (x for x in entries if x.get("kind") == STARTED and x.get("attempt_id") == accepted.get("attempt_id")), None
    )
    if not started or not _scoped(started, row) or started.get("binding") != accepted.get("binding"):
        return None
    return accepted


def begin(row, *, expected_binding, grant_kind, content_type, native_resume):
    """Commit intent before provider I/O, or fail closed on an uncertain prior write."""
    with transaction.atomic():
        current = (
            PlatformPost.objects.select_for_update().select_related("post__workspace", "social_account").get(pk=row.pk)
        )
        if (
            current.status != PlatformPost.Status.PUBLISHING
            or current.post.workspace.is_archived
            or current.social_account.needs_reconnect
            or current.social_account.workspace_id != current.post.workspace_id
            or binding(current, grant_kind=grant_kind, content_type=content_type) != expected_binding
        ):
            raise PublishError("Meta publish inputs or authorization changed before submission.", retryable=False)
        accepted = accepted_attempt(current)
        if accepted:
            if accepted["binding"] != expected_binding:
                raise PublishError(
                    "Previous Meta receipt belongs to different publish inputs; reconcile before retrying.",
                    retryable=False,
                )
            return {"marker": accepted, "replay": True}
        entries = _entries(current)
        started = next((x for x in entries if x.get("kind") == STARTED), None)
        phase = (current.native_upload_state or {}).get("progress", {}).get("phase")
        if started:
            if not _scoped(started, current) or started.get("binding") != expected_binding:
                raise PublishError(
                    "Unconfirmed Meta attempt has different publish inputs; reconcile before retrying.", retryable=False
                )
            if not native_resume or phase not in {"uploaded", "complete"}:
                raise PublishError(
                    "Previous Meta submission outcome is unconfirmed; no publish request was repeated.", retryable=False
                )
            return {"marker": started, "recovered": phase == "complete"}
        if current.native_upload_state:
            raise PublishError(
                "Legacy native upload has no current attempt provenance; reconcile before retrying.", retryable=False
            )
        marker = {
            "kind": STARTED,
            "attempt_id": str(uuid.uuid4()),
            "child_id": str(current.pk),
            "account_id": str(current.social_account_id),
            "workspace_id": str(current.post.workspace_id),
            "owner_id": current.social_account.account_platform_id,
            "platform": current.social_account.platform,
            "grant_kind": grant_kind,
            "content_type": content_type,
            "binding": expected_binding,
        }
        PublishLog.objects.create(
            platform_post=current,
            attempt_number=current.retry_count + 1,
            status_code=None,
            response_body=json.dumps(marker, separators=(",", ":")),
        )
        return {"marker": marker}


def accept(row, result, *, duration_ms):
    """Atomically retain the receipt and audit; never promote it to published."""
    context = result["meta_attempt"]
    marker = context["marker"]
    receipt = result.get("platform_post_id")
    if not native_id(receipt):
        raise PublishError("Meta returned no valid native receipt; reconcile before retrying.", retryable=False)
    public_id = result.get("public_post_id")
    public_id = public_id if native_id(public_id) else receipt
    with transaction.atomic():
        current = (
            PlatformPost.objects.select_for_update().select_related("post__workspace", "social_account").get(pk=row.pk)
        )
        entries = _entries(current)
        started = next(
            (x for x in entries if x.get("kind") == STARTED and x.get("attempt_id") == marker.get("attempt_id")), None
        )
        if not started or started.get("binding") != marker.get("binding"):
            raise PublishError(
                "Meta receipt has no durable submission intent; reconcile before retrying.", retryable=False
            )
        accepted = {
            **started,
            "kind": RECOVERED if context.get("recovered") else ACCEPTED,
            "receipt_id": receipt,
            "public_post_id": public_id,
        }
        if not context.get("replay"):
            PublishLog.objects.create(
                platform_post=current,
                attempt_number=current.retry_count + 1,
                status_code=None if context.get("recovered") else result.get("status_code", 200),
                response_body=json.dumps(accepted, separators=(",", ":")),
                duration_ms=duration_ms,
            )
        # Preserve owner changes/holds and every checkpoint saved by the provider.
        if current.status == PlatformPost.Status.PUBLISHING and current.platform_post_id in {"", receipt}:
            current.platform_post_id = receipt
            current.retry_count = 0
            current.next_retry_at = None
            current.publish_error = ""
            current.save(
                update_fields=["platform_post_id", "retry_count", "next_retry_at", "publish_error", "updated_at"]
            )
        row.refresh_from_db()
