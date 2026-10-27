"""Durable, internal native-video checkpoints. No locks span network calls."""

import hashlib
import json
import re

from django.db import transaction

from apps.composer.models import PlatformPost
from providers.exceptions import PublishError


class NativeUploadCache:
    """Compare-and-set adapter scoped to one engine-owned publishing child.

    Checkpoints contain identifiers and hashes only. This is not an HTTP audit
    and is never merged into platform_extra or serialized through the API.
    """

    def __init__(self, platform_post):
        self.pk = platform_post.pk
        self.binding = self._binding(platform_post)
        self.seen = None
        self.loaded = False

    @staticmethod
    def _binding(row):
        account = row.social_account
        token = (
            account.oauth_facebook_user_access_token if account.platform == "instagram" else account.oauth_access_token
        )
        return (
            str(row.post_id),
            str(row.post.workspace_id),
            str(row.post.author_id),
            str(row.social_account_id),
            account.platform,
            str(account.account_platform_id),
            row.effective_caption,
            hashlib.sha256(str(token or "").encode()).hexdigest(),
            str(account.facebook_user_token_expires_at) if account.platform == "instagram" else None,
            json.dumps(row.platform_specific_media, sort_keys=True),
            json.dumps(row.platform_extra, sort_keys=True),
            tuple(
                row.post.media_attachments.order_by("position", "pk").values_list(
                    "media_asset_id", "media_asset__file", "media_asset__workspace_id", "media_asset__organization_id"
                )
            ),
        )

    def _locked(self):
        row = (
            PlatformPost.objects.select_for_update()
            .select_related("post__workspace", "social_account__workspace")
            .get(pk=self.pk)
        )
        if (
            row.status != PlatformPost.Status.PUBLISHING
            or row.platform_post_id
            or row.published_at
            or row.post.workspace.is_archived
            or row.social_account.needs_reconnect
            or row.social_account.workspace_id != row.post.workspace_id
            or any(
                asset_workspace != row.post.workspace_id
                and not (asset_workspace is None and asset_org == row.post.workspace.organization_id)
                for _, _, asset_workspace, asset_org in self._binding(row)[-1]
            )
            or self._binding(row) != self.binding
        ):
            raise PublishError(
                "Native upload authorization or content changed; reconcile before retrying.", retryable=False
            )
        return row

    def get(self, key):
        with transaction.atomic():
            row = self._locked()
            self.loaded = True
            self.seen = row.native_upload_state
            if not self.seen:
                return None
            if self.seen.get("key") != key:
                raise PublishError(
                    "Native upload content changed; reconcile the existing session before retrying.", retryable=False
                )
            return json.dumps(self.seen["progress"])

    def set(self, key, value, timeout):
        progress = json.loads(value)
        if (
            set(progress) - {"phase", "upload_id", "media_id", "expires_at"}
            or progress.get("phase") not in {"creating", "created", "uploading", "uploaded", "publishing", "complete"}
            or any(not isinstance(v, str) or len(v) > 255 for v in progress.values())
        ):
            raise PublishError("Invalid native upload checkpoint.", retryable=False)
        with transaction.atomic():
            row = self._locked()
            previous = (self.seen or {}).get("progress", {}).get("phase")
            transitions = {
                None: "creating",
                "creating": "created",
                "created": "uploading",
                "uploading": "uploaded",
                "uploaded": "publishing",
                "publishing": "complete",
            }
            if (
                not self.loaded
                or not re.fullmatch(r"[a-f0-9]{64}", key)
                or transitions.get(previous) != progress["phase"]
            ):
                raise PublishError("Invalid native upload phase transition.", retryable=False)
            if progress["phase"] != "creating" and not re.fullmatch(r"[A-Za-z0-9_-]+", progress.get("upload_id", "")):
                raise PublishError("Invalid native upload identifier.", retryable=False)
            if progress["phase"] == "complete" and not re.fullmatch(r"[A-Za-z0-9_-]+", progress.get("media_id", "")):
                raise PublishError("Invalid native publication identifier.", retryable=False)
            if previous not in (None, "creating") and progress.get("upload_id") != self.seen["progress"].get(
                "upload_id"
            ):
                raise PublishError("Native upload session changed.", retryable=False)
            if row.native_upload_state != self.seen:
                raise PublishError("Native upload ownership changed; reconcile before retrying.", retryable=False)
            state = {"key": key, "progress": progress}
            row.native_upload_state = state
            row.save(update_fields=["native_upload_state"])
            self.seen = state
