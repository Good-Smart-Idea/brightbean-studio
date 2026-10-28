"""Internal append-only Meta attempt provenance, separate from upload progress.

STARTED has no HTTP status: it is committed before any provider invocation.
ACCEPTED records a returned receipt; RECOVERED records durable-cache recovery
without pretending another HTTP request occurred. None are client API input.
"""

import hashlib
import json
import re
import uuid

from django.db import transaction
from django.db.models import Q

from apps.composer.models import PlatformPost
from apps.publisher.models import PublishLog
from providers.exceptions import PublishError
from providers.meta_confirmation import native_id

STARTED = "meta_publish_started"
ACCEPTED = "meta_publish_accepted"
RECOVERED = "meta_publish_recovered"


def binding(platform_post, *, grant_kind, content_type, manifest, attachments=None):
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
        manifest,
    ]
    return hashlib.sha256(json.dumps(inputs, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _hash(stream):
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        if size > 1024 * 1024 * 1024:
            raise PublishError("Native media exceeds the bounded byte-verification limit.", retryable=False)
        digest.update(chunk)
    return digest.hexdigest(), size


def prepared_manifest(attachments, files):
    assets = [pm.media_asset for pm in attachments if pm.media_asset.file]
    if not files or len(files) != len(assets):
        return []  # Hosted transport has no actual byte proof.
    manifest = []
    for asset, path in zip(assets, files, strict=True):
        with open(path, "rb") as stream:
            digest, size = _hash(stream)
        manifest.append({"asset_id": str(asset.pk), "sha256": digest, "size": size})
    return manifest


def verify_media(row, manifest):
    """Private storage reads occur outside any ledger row-lock transaction."""
    if not isinstance(manifest, list) or not manifest:
        return False
    assets = [
        pm.media_asset
        for pm in row.post.media_attachments.select_related("media_asset").order_by("position", "pk")
        if pm.media_asset.file
    ]
    if len(assets) != len(manifest):
        return False
    for asset, expected in zip(assets, manifest, strict=True):
        if not isinstance(expected, dict) or expected.get("asset_id") != str(asset.pk):
            return False
        try:
            with asset.file.open("rb") as stream:
                digest, size = _hash(stream)
        except Exception:
            # Storage failures are unconfirmed identity, never publication proof.
            return False
        if expected.get("sha256") != digest or expected.get("size") != size or size <= 0:
            return False
    return True


def _media_scope_valid(row):
    return all(
        workspace == row.post.workspace_id or (workspace is None and organization == row.post.workspace.organization_id)
        for workspace, organization in row.post.media_attachments.values_list(
            "media_asset__workspace_id", "media_asset__organization_id"
        )
    )


def _encode(value):
    if not _valid_marker(value):
        raise PublishError(
            "Invalid internal Meta attempt provenance; no publish request was repeated.", retryable=False
        )
    body = json.dumps(value, separators=(",", ":"))
    if len(body) > 1000:
        raise PublishError("Meta attempt provenance exceeds the bounded audit limit.", retryable=False)
    return body


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _valid_marker(value):
    if not isinstance(value, dict):
        return False
    common = {
        "kind",
        "attempt_id",
        "child_id",
        "account_id",
        "workspace_id",
        "owner_id",
        "platform",
        "grant_kind",
        "content_type",
        "binding",
        "manifest",
        "media_bytes_verified",
    }
    kind = value.get("kind")
    if kind not in {STARTED, ACCEPTED, RECOVERED}:
        return False
    wanted = common if kind == STARTED else common | {"receipt_id", "public_post_id"}
    if set(value) != wanted or not all(
        _uuid(value.get(k)) for k in ("attempt_id", "child_id", "account_id", "workspace_id")
    ):
        return False
    digest = value.get("binding")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or not native_id(value.get("owner_id")):
        return False
    platform = value.get("platform")
    if platform not in {"facebook", "instagram"}:
        return False
    if value.get("grant_kind") not in ({"account", "facebook_user"} if platform == "instagram" else {"account"}):
        return False
    modes = {"video", "reel"} if platform == "facebook" else {"image", "carousel", "video", "reel"}
    if value.get("content_type") not in modes or type(value.get("media_bytes_verified")) is not bool:
        return False
    manifest = value.get("manifest")
    if not isinstance(manifest, list) or len(manifest) > 10 or bool(manifest) != value["media_bytes_verified"]:
        return False
    ids = set()
    for item in manifest:
        if not isinstance(item, dict) or set(item) != {"asset_id", "sha256", "size"} or not _uuid(item.get("asset_id")):
            return False
        sha = item.get("sha256")
        if (
            not isinstance(sha, str)
            or not re.fullmatch(r"[a-f0-9]{64}", sha)
            or type(item.get("size")) is not int
            or not 0 < item["size"] <= 1024**3
        ):
            return False
        if item["asset_id"] in ids:
            return False
        ids.add(item["asset_id"])
    return kind == STARTED or (native_id(value.get("receipt_id")) and native_id(value.get("public_post_id")))


def _entries(row):
    entries = []
    typed = Q()
    for kind in (STARTED, ACCEPTED, RECOVERED):
        typed |= Q(response_body__startswith='{"kind":"' + kind + '"')
    for log in row.publish_logs.filter(typed).order_by("-created_at")[:30]:
        try:
            value = json.loads(log.response_body)
        except (TypeError, ValueError):
            continue
        if not _valid_marker(value) or log.error_message:
            continue
        if value["kind"] == ACCEPTED and not (isinstance(log.status_code, int) and 200 <= log.status_code < 300):
            continue
        if value["kind"] in {STARTED, RECOVERED} and log.status_code is not None:
            continue
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
    if not started or not _scoped(started, row):
        return None
    if any(started[k] != accepted[k] for k in started if k != "kind"):
        return None
    if row.platform_post_id and row.platform_post_id != accepted["receipt_id"]:
        return None
    return accepted


def begin(row, *, expected_binding, grant_kind, content_type, manifest, native_resume):
    """Commit intent before provider I/O, or fail closed on an uncertain prior write."""
    bytes_verified = verify_media(row, manifest) if manifest else False
    with transaction.atomic():
        current = (
            PlatformPost.objects.select_for_update().select_related("post__workspace", "social_account").get(pk=row.pk)
        )
        if (
            current.status != PlatformPost.Status.PUBLISHING
            or current.post.workspace.is_archived
            or current.social_account.needs_reconnect
            or current.social_account.workspace_id != current.post.workspace_id
            or not _media_scope_valid(current)
            or binding(current, grant_kind=grant_kind, content_type=content_type, manifest=manifest) != expected_binding
        ):
            raise PublishError("Meta publish inputs or authorization changed before submission.", retryable=False)
        if native_resume and not bytes_verified:
            raise PublishError(
                "Native media bytes do not match the prepared asset; no publish request was sent.", retryable=False
            )
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
        if current.platform_post_id:
            raise PublishError(
                "Existing Meta receipt has no trusted attempt provenance; reconcile before retrying.", retryable=False
            )
        if current.native_upload_state:
            raise PublishError(
                "Legacy native upload has no current attempt provenance; reconcile before retrying.", retryable=False
            )
        if native_resume and not bytes_verified:
            raise PublishError(
                "Native media bytes do not match the prepared asset; no publish request was sent.", retryable=False
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
            "manifest": manifest,
            "media_bytes_verified": bytes_verified,
        }
        PublishLog.objects.create(
            platform_post=current,
            attempt_number=current.retry_count + 1,
            status_code=None,
            response_body=_encode(marker),
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
    bytes_match = verify_media(row, marker.get("manifest")) if marker.get("media_bytes_verified") else True
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
                response_body=_encode(accepted),
                duration_ms=duration_ms,
            )
        # Preserve owner changes/holds and every checkpoint saved by the provider.
        if (
            current.status == PlatformPost.Status.PUBLISHING
            and current.platform_post_id in {"", receipt}
            and _scoped(started, current)
            and bytes_match
            and not current.post.workspace.is_archived
            and not current.social_account.needs_reconnect
            and binding(
                current,
                grant_kind=started["grant_kind"],
                content_type=started["content_type"],
                manifest=started.get("manifest", []),
            )
            == started["binding"]
        ):
            current.platform_post_id = receipt
            current.retry_count = 0
            current.next_retry_at = None
            current.publish_error = ""
            current.save(
                update_fields=["platform_post_id", "retry_count", "next_retry_at", "publish_error", "updated_at"]
            )
        row.refresh_from_db()
