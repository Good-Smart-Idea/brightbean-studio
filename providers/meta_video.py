"""Native Meta video upload validation; no network or credential refresh.

Protocol references: Meta's fbsamples/reels_publishing_apis Instagram sample
index.js lines 296-335, and Facebook sample index.js lines 192-205.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import time
from urllib.parse import urlsplit

from .exceptions import APIError, PublishError, RateLimitError, TokenExpiredError

# Diagnostics are evidence about one operation, never proof of publication.
_DIAGNOSTIC_KIND = "native_video_write_failure"
_STAGES = frozenset(
    {
        "resume_guard",
        "create_response",
        "create_ack",
        "upload_uri_validation",
        "upload_http",
        "upload_ack",
        "publish_response",
        "publish_ack",
        "checkpoint_creating",
        "checkpoint_created",
        "checkpoint_uploading",
        "checkpoint_uploaded",
        "checkpoint_publishing",
        "checkpoint_complete",
    }
)
_CAUSES = frozenset({"api", "rate_limit", "token_expired", "publish", "transport", "unknown"})
_URI_FLAGS = frozenset(
    {
        "present",
        "string",
        "https",
        "exact_host",
        "terminal_id_match",
        "query",
        "userinfo",
        "port",
        "fragment",
    }
)


def _bounded_int(value, low=0, high=2**31 - 1):
    return value if type(value) is int and low <= value <= high else None


def _uri_diagnostic(uri, upload_id):
    """Classify the returned destination without retaining any URI text."""
    result = dict.fromkeys(_URI_FLAGS, False)
    result.update(present=uri is not None, string=type(uri) is str, path_kind="unknown", api_version=None)
    if type(uri) is not str:
        return result
    try:
        parsed = urlsplit(uri)
        parts = parsed.path.split("/")
        result.update(
            https=parsed.scheme == "https",
            exact_host=parsed.hostname == "rupload.facebook.com",
            query=bool(parsed.query),
            userinfo="@" in parsed.netloc,
            port=":" in parsed.netloc.rsplit("@", 1)[-1],
            fragment=bool(parsed.fragment),
            terminal_id_match=type(upload_id) is str and parts[-1] == upload_id,
        )
        if len(parts) > 1 and parts[1] in {"video-upload", "ig-api-upload"}:
            result["path_kind"] = parts[1]
        if len(parts) > 2 and (match := re.fullmatch(r"v([0-9]{1,3})\.([0-9]{1,3})", parts[2])):
            result["api_version"] = [int(match[1]), int(match[2])]
    except Exception:
        # Even a malformed destination must still reach the original validator.
        pass
    return result


def _failure_diagnostic(stage, exc=None, *, uri=None, status=None):
    """Only closed types survive; hostile getters cannot hide the original failure."""
    result = {
        "kind": _DIAGNOSTIC_KIND,
        "schema_version": 1,
        "stage": stage,
        "http_status": _bounded_int(status, 100, 599),
        "provider_code": None,
        "provider_subcode": None,
        "cause_kind": "unknown",
        "cause_retryable": None,
        "uri": uri,
    }
    try:
        import httpx

        if isinstance(exc, TokenExpiredError):
            result["cause_kind"] = "token_expired"
        elif isinstance(exc, RateLimitError):
            result["cause_kind"] = "rate_limit"
        elif isinstance(exc, APIError):
            result["cause_kind"] = "api"
        elif isinstance(exc, PublishError):
            result["cause_kind"] = "publish"
        elif isinstance(exc, httpx.TransportError):
            result["cause_kind"] = "transport"
        retryable = getattr(exc, "retryable", None)
        result["cause_retryable"] = retryable if type(retryable) is bool else None
        if result["http_status"] is None:
            result["http_status"] = _bounded_int(getattr(exc, "status_code", None), 100, 599)
        raw = getattr(exc, "raw_response", None)
        error = raw.get("error") if type(raw) is dict else None
        if type(error) is dict:
            result["provider_code"] = _bounded_int(error.get("code"))
            result["provider_subcode"] = _bounded_int(error.get("error_subcode"))
    except Exception:
        pass
    return result


def native_failure_log_fields(exc):
    """Fail closed before persisting the optional diagnostic in PublishLog."""
    try:
        raw = getattr(exc, "_native_video_diagnostic", None)
        if raw is None:
            raw = getattr(exc, "raw_response", None)
        keys = {
            "kind",
            "schema_version",
            "stage",
            "http_status",
            "provider_code",
            "provider_subcode",
            "cause_kind",
            "cause_retryable",
            "uri",
        }
        if (
            type(raw) is not dict
            or any(type(key) is not str for key in raw)
            or set(raw) != keys
            or type(raw["kind"]) is not str
            or raw["kind"] != _DIAGNOSTIC_KIND
        ):
            return {}
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            return {}
        if type(raw["stage"]) is not str or raw["stage"] not in _STAGES:
            return {}
        if type(raw["cause_kind"]) is not str or raw["cause_kind"] not in _CAUSES:
            return {}
        if raw["cause_retryable"] is not None and type(raw["cause_retryable"]) is not bool:
            return {}
        for field, low, high in (
            ("http_status", 100, 599),
            ("provider_code", 0, 2**31 - 1),
            ("provider_subcode", 0, 2**31 - 1),
        ):
            if raw[field] is not None and _bounded_int(raw[field], low, high) is None:
                return {}
        uri = raw["uri"]
        if uri is not None:
            if (
                type(uri) is not dict
                or any(type(key) is not str for key in uri)
                or set(uri) != _URI_FLAGS | {"path_kind", "api_version"}
            ):
                return {}
            if any(type(uri[key]) is not bool for key in _URI_FLAGS):
                return {}
            if type(uri["path_kind"]) is not str or uri["path_kind"] not in {
                "unknown",
                "video-upload",
                "ig-api-upload",
            }:
                return {}
            version = uri["api_version"]
            if version is not None and (
                type(version) is not list
                or len(version) != 2
                or any(_bounded_int(part, 0, 999) is None for part in version)
            ):
                return {}
        body = json.dumps(raw, separators=(",", ":"))
        return {"response_body": body, "status_code": raw["http_status"]} if len(body) <= 1000 else {}
    except Exception:
        return {}


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
        paths = {f"/{kind}/{version}/{upload_id}", f"/{kind}/{upload_id}"}
        # Graph v25 creates can return the distinct v26 upload protocol.
        v26_path = f"/{kind}/v26.0/{upload_id}"
        if (
            version == "v25.0"
            and kind in {"video-upload", "ig-api-upload"}
            and uri == f"https://rupload.facebook.com{v26_path}"
        ):
            paths.add(v26_path)
        invalid = (
            parsed.scheme != "https"
            or parsed.hostname != "rupload.facebook.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in paths
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
    if state.get("phase") == "complete":
        media_id = state.get("media_id")
        if not isinstance(media_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", media_id):
            raise PublishError(
                "Invalid native publication checkpoint; reconcile before retrying.", platform=platform, retryable=False
            )
        return PublishResult(platform_post_id=media_id, extra={"video_id": media_id}, needs_confirmation=True)
    # A confirmed receipt needs no live grant. Incomplete sessions must be
    # authorized even before read-only processing polls, and again per POST.
    if authorize is not None:
        authorize()
    expires_at = state.get("expires_at") or str(int(time.time()) + 3600)
    if int(expires_at) <= time.time():
        raise PublishError(
            "Native upload session expired; reconcile before retrying.", platform=platform, retryable=False
        )

    uri_diagnostic = None

    def checkpoint(phase, **ids):
        nonlocal state
        state = {"phase": phase, "expires_at": expires_at, **ids}
        try:
            cache.set(key, json.dumps(state), 86400)
        except Exception as exc:
            # Preserve the same exception and retry policy outside write catches.
            with contextlib.suppress(Exception):
                exc._native_video_diagnostic = _failure_diagnostic("checkpoint_" + phase, exc, uri=uri_diagnostic)
            raise

    def stop_before_write():
        if authorize is not None:
            authorize()
        if content.should_stop and content.should_stop():
            raise PublishError("Native video interrupted before the next provider write.", platform=platform)

    def unconfirmed(stage="resume_guard", exc=None, *, status=None):
        return PublishError(
            "Native video write outcome is unconfirmed; reconcile this session before retrying.",
            platform=platform,
            retryable=False,
            raw_response=_failure_diagnostic(stage, exc, uri=uri_diagnostic, status=status),
        )

    if state and state.get("phase") != "uploaded":
        raise unconfirmed()
    if not state:
        stop_before_write()
        checkpoint("creating")
        stage = "create_response"
        try:
            upload_id, uri = create()
            stage = "create_ack"
            uri_diagnostic = _uri_diagnostic(uri, upload_id)
            if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", upload_id):
                raise unconfirmed()
            stage = "checkpoint_created"
            checkpoint("created", upload_id=upload_id)
            stage = "upload_uri_validation"
            uri = validate_meta_upload_uri(uri, upload_id, kind=kind, platform=platform)
        except Exception as exc:
            raise unconfirmed(stage, exc) from None
        stop_before_write()
        checkpoint("uploading", upload_id=upload_id)
        stage = "upload_http"
        status = None
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
            stage = "upload_ack"
            status = response.status_code
            if not 200 <= response.status_code < 300 or response.json().get("success") is not True:
                raise unconfirmed()
        except Exception as exc:
            raise unconfirmed(stage, exc, status=status) from None
        checkpoint("uploaded", upload_id=upload_id)
    upload_id = state["upload_id"]
    wait(upload_id)
    stop_before_write()
    checkpoint("publishing", upload_id=upload_id)
    stage = "publish_response"
    try:
        result = publish(upload_id)
        stage = "publish_ack"
        if not result.platform_post_id:
            raise unconfirmed()
    except Exception as exc:
        raise unconfirmed(stage, exc) from None
    checkpoint("complete", upload_id=upload_id, media_id=result.platform_post_id)
    return result
