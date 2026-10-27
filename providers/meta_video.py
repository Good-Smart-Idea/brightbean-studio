"""Native Meta video upload validation; no network or credential refresh.

Protocol references: Meta's fbsamples/reels_publishing_apis Instagram sample
index.js lines 296-335, and Facebook sample index.js lines 192-205.
"""

from __future__ import annotations

import math
import re
import time
from urllib.parse import urlsplit

from .exceptions import PublishError

_MESSAGES = {
    "disconnected_account": "Reconnect this social account before uploading a video.",
    "missing_instagram_target": "The Instagram connection has no bound account ID.",
    "missing_facebook_user_grant": "Reconnect Instagram through Facebook to restore its video-upload user grant.",
    "expired_facebook_user_grant": "The Facebook user grant for Instagram video upload has expired. Reconnect Instagram through Facebook.",
    "invalid_facebook_user_grant_expiry": "The Instagram user-grant expiry could not be checked. Reconnect Instagram through Facebook.",
    "missing_page_target": "The Facebook connection has no bound Page ID.",
    "missing_page_grant": "Reconnect the Facebook Page to restore its video-upload grant.",
}


def instagram_native_configuration(credentials, *, now=None):
    """Presence/known-expiry check, not a live Meta eligibility verification."""
    blocker = ""
    if credentials.get("native_video_account_connected") is False:
        blocker = "disconnected_account"
    elif not credentials.get("ig_user_id"):
        blocker = "missing_instagram_target"
    elif not credentials.get("facebook_user_access_token"):
        blocker = "missing_facebook_user_grant"
    else:
        expiry = credentials.get("facebook_user_token_expires_at")
        if expiry is not None:
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                blocker = "invalid_facebook_user_grant_expiry"
            elif expiry <= (time.time() if now is None else now):
                blocker = "expired_facebook_user_grant"
    return {"supported": True, "configured": not blocker, "blocker": blocker, "provider_eligibility_verified": False}


def facebook_native_configuration(credentials, page_token):
    blocker = ""
    if credentials.get("native_video_account_connected") is False:
        blocker = "disconnected_account"
    elif not credentials.get("page_id"):
        blocker = "missing_page_target"
    elif not page_token:
        blocker = "missing_page_grant"
    return {"supported": True, "configured": not blocker, "blocker": blocker, "provider_eligibility_verified": False}


def require_instagram_native_token(credentials):
    status = instagram_native_configuration(credentials)
    if not status["configured"]:
        raise PublishError(_MESSAGES[str(status["blocker"])], platform="Instagram", retryable=False)
    return credentials["facebook_user_access_token"]


def require_facebook_native_configuration(credentials, page_token):
    status = facebook_native_configuration(credentials, page_token)
    if not status["configured"]:
        raise PublishError(_MESSAGES[str(status["blocker"])], platform="Facebook", retryable=False)


def validate_meta_upload_uri(uri, upload_id, *, kind, version="v25.0", platform):
    """Validate before any credential or body is sent; never echo an unsafe URI."""
    if not isinstance(uri, str) or not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", upload_id):
        raise PublishError("Meta returned an invalid video upload session.", platform=platform, retryable=False)
    try:
        parsed = urlsplit(uri)
        invalid = (
            parsed.scheme != "https"
            or parsed.hostname != "rupload.facebook.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {f"/{kind}/{version}/{upload_id}", f"/{kind}/{upload_id}"}
        )
    except ValueError:
        invalid = True
    if invalid:
        raise PublishError("Meta returned an unsafe video upload destination.", platform=platform, retryable=False)
    return uri


def publish_native_video(provider, content, *, target, token, kind, create, wait, publish, authorize=None):
    """Stream one file, checkpoint before writes, and resume only safe reads.

    A network/body/final-publish outcome is ambiguous until its checkpoint is
    committed. Never repeat those writes on a new attempt. Poll timeouts keep
    the uploaded container and can safely resume with the same creation ID.
    """
    import hashlib
    import json
    import os

    from .types import PublishResult

    platform = provider.platform_name
    if len(content.media_files) != 1 or not content.upload_cache or not content.idempotency_key:
        raise PublishError(
            "Native video requires one local asset and durable progress storage.", platform=platform, retryable=False
        )
    path = content.media_files[0]
    size = os.path.getsize(path)
    if not 0 < size <= 1024 * 1024 * 1024:
        raise PublishError("Native video must be between one byte and 1 GB.", platform=platform, retryable=False)
    digest = hashlib.sha256()
    with open(path, "rb") as body:
        while chunk := body.read(1024 * 1024):
            digest.update(chunk)
    key = hashlib.sha256(
        json.dumps(
            [
                content.idempotency_key,
                target,
                kind,
                content.text,
                content.post_type.value,
                size,
                digest.hexdigest(),
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    cache = content.upload_cache
    raw = cache.get(key)
    state = json.loads(raw) if raw else {}
    expires_at = state.get("expires_at") or str(int(time.time()) + 3600)
    if int(expires_at) <= time.time():
        raise PublishError(
            "Native upload session expired; reconcile before retrying.", platform=platform, retryable=False
        )

    def checkpoint(phase, **ids):
        nonlocal state
        state = {"phase": phase, "expires_at": expires_at, **ids}
        cache.set(key, json.dumps(state), 86400)

    def stop_before_write():
        if authorize is not None:
            authorize()
        if content.should_stop and content.should_stop():
            raise PublishError("Native video interrupted before the next provider write.", platform=platform)

    def unconfirmed():
        return PublishError(
            "Native video write outcome is unconfirmed; reconcile this session before retrying.",
            platform=platform,
            retryable=False,
        )

    if state.get("phase") == "complete":
        return PublishResult(platform_post_id=state["media_id"], extra={"video_id": state["media_id"]})
    if state and state.get("phase") != "uploaded":
        raise unconfirmed()
    if not state:
        stop_before_write()
        checkpoint("creating")
        try:
            upload_id, uri = create()
            if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", upload_id):
                raise unconfirmed()
            checkpoint("created", upload_id=upload_id)
            uri = validate_meta_upload_uri(uri, upload_id, kind=kind, platform=platform)
        except Exception:
            raise unconfirmed() from None
        stop_before_write()
        checkpoint("uploading", upload_id=upload_id)
        try:
            with open(path, "rb") as body:
                response = provider._request(
                    "POST",
                    uri,
                    data=body,
                    headers={
                        "Authorization": f"OAuth {token}",
                        "offset": "0",
                        "file_size": str(size),
                        "Content-Length": str(size),
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=120,
                )
            if not 200 <= response.status_code < 300 or response.json().get("success") is not True:
                raise unconfirmed()
        except Exception:
            raise unconfirmed() from None
        checkpoint("uploaded", upload_id=upload_id)
    upload_id = state["upload_id"]
    wait(upload_id)
    stop_before_write()
    checkpoint("publishing", upload_id=upload_id)
    try:
        result = publish(upload_id)
        if not result.platform_post_id:
            raise unconfirmed()
    except Exception:
        raise unconfirmed() from None
    checkpoint("complete", upload_id=upload_id, media_id=result.platform_post_id)
    return result
