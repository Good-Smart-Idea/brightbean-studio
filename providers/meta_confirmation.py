"""Read-only Meta receipt confirmation; no upload, finish, refresh or retry."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .types import PublishState, PublishStatus


def native_id(value):
    return isinstance(value, str) and 0 < len(value) <= 64 and value.isascii() and value.isdecimal()


def facebook_story_id(value, owner_id):
    """A separate photo-story identifier; numeric native_id remains unchanged."""
    if type(value) is not str or not 0 < len(value) <= 129 or not native_id(owner_id):
        return False
    if native_id(value):
        return True
    parts = value.split("_")
    return len(parts) == 2 and parts[0] == owner_id and native_id(parts[1])


def facebook_photo_status(provider, token, handle):
    """Exact v25 Photo -> retained Post proof; no guessed IDs or writes."""
    target = provider.credentials.get("page_id")
    story_id = provider.credentials.get("native_photo_story_id")
    caption = provider.credentials.get("native_photo_caption")
    if (
        not native_id(target)
        or not native_id(handle)
        or not facebook_story_id(story_id, target)
        or type(caption) is not str
    ):
        return pending("Facebook photo confirmation context is unavailable.")
    host = "https://graph.facebook.com/v25.0"
    try:
        photo = native_read(provider, f"{host}/{handle}", token, {"fields": "id,from,name,page_story_id"})
    except Exception:
        return pending("Facebook photo evidence could not be read.")
    if type(photo) is not dict or photo.get("id") != handle:
        return pending("Facebook did not return the exact photo receipt.")
    owner = photo.get("from")
    if type(owner) is not dict or owner.get("id") != target:
        return pending("Facebook photo ownership is unconfirmed.")
    if photo.get("name") != caption or type(photo.get("name")) is not str or photo.get("page_story_id") != story_id:
        return pending("Facebook photo caption or retained story is unconfirmed.")
    try:
        post = native_read(
            provider, f"{host}/{story_id}", token, {"fields": "id,from,message,is_published,is_hidden,permalink_url"}
        )
    except Exception:
        return pending("Facebook photo story evidence could not be read.")
    if type(post) is not dict or post.get("id") != story_id:
        return pending("Facebook did not return the exact retained photo story.")
    owner = post.get("from")
    link = permalink(post.get("permalink_url"), facebook=True)
    if (
        type(owner) is not dict
        or owner.get("id") != target
        or type(post.get("message")) is not str
        or post["message"] != caption
        or post.get("is_published") is not True
        or post.get("is_hidden") is not False
        or not link
    ):
        return pending("Facebook photo story publication is unconfirmed.")
    return PublishStatus(
        PublishState.COMPLETE,
        platform_post_id=story_id,
        raw={
            "id": handle,
            "owner_id": target,
            "story_id": story_id,
            "caption_verified": True,
            "is_published": True,
            "is_hidden": False,
            "permalink": link,
        },
    )


def permalink(value, *, facebook):
    if not isinstance(value, str) or len(value) > 512:
        return ""
    if facebook and value.startswith("/") and not value.startswith("//"):
        value = "https://www.facebook.com" + value
    try:
        parsed = urlsplit(value)
        hosts = {"facebook.com", "www.facebook.com"} if facebook else {"instagram.com", "www.instagram.com"}
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or not parsed.path.strip("/")
        ):
            return ""
        if not facebook and not parsed.path.startswith(("/p/", "/reel/", "/tv/")):
            return ""
    except ValueError:
        return ""
    query = ""
    if facebook:
        allowed = [
            (key, val)
            for key, val in parse_qsl(parsed.query)
            if key in {"v", "id", "story_fbid", "fbid"} and native_id(val)
        ]
        values = dict(allowed)
        special = parsed.path.rstrip("/")
        required = {
            "/watch": "v",
            "/video.php": "v",
            "/photo.php": "fbid",
            "/story.php": "story_fbid",
            "/permalink.php": "story_fbid",
        }
        if special in required:
            if required[special] not in values:
                return ""
        elif not any(
            native_id(part) or (part.startswith("pfbid") and part.isascii() and part.isalnum() and len(part) > 20)
            for part in parsed.path.strip("/").split("/")
        ):
            return ""
        query = urlencode(allowed)
    return urlunsplit(("https", parsed.hostname, parsed.path, query, ""))


def pending(reason):
    return PublishStatus(PublishState.UNCONFIRMED, error=reason)


def native_read(provider, url, token, params):
    response = provider._request("GET", url, access_token=token, params=params)
    if response.status_code != 200:
        return None
    return response.json()


def facebook_video_status(provider, token, handle):
    target = str(provider.credentials.get("page_id") or "")
    if not native_id(target) or not native_id(handle):
        return PublishStatus(PublishState.FAILED, error="Facebook video receipt or bound Page is invalid.")
    row = native_read(
        provider,
        f"https://graph.facebook.com/v25.0/{handle}",
        token,
        {"fields": "id,status,published,from,permalink_url"},
    )
    if not isinstance(row, dict) or str(row.get("id") or "") != handle:
        return pending("Facebook did not return the exact video receipt.")
    owner = row.get("from")
    if not isinstance(owner, dict) or not native_id(str(owner.get("id") or "")):
        return pending("Facebook video owner is not confirmed.")
    if str(owner["id"]) != target:
        return PublishStatus(PublishState.FAILED, error="Facebook video belongs to a different Page.")
    status = row.get("status")
    if not isinstance(status, dict):
        return pending("Facebook video processing status is unavailable.")
    video_status = str(status.get("video_status") or "").lower()
    phases = [status.get(name) for name in ("uploading_phase", "processing_phase", "publishing_phase")]
    if video_status in {"error", "failed"} or any(
        isinstance(x, dict) and str(x.get("status") or "").lower() in {"error", "failed"} for x in phases
    ):
        return PublishStatus(PublishState.FAILED, error="Facebook rejected video processing or publication.")
    link = permalink(row.get("permalink_url"), facebook=True)
    if (
        video_status not in {"ready", "processing", "uploading"}
        or not isinstance(row.get("published"), bool)
        or not link
    ):
        return pending("Facebook native publication evidence is unavailable.")
    if video_status != "ready" or row.get("published") is not True:
        return PublishStatus(PublishState.PENDING, error="Facebook video is still processing or unpublished.")
    return PublishStatus(
        PublishState.COMPLETE,
        platform_post_id=handle,
        raw={"id": handle, "owner_id": target, "published": True, "video_status": "ready", "permalink": link},
    )


def instagram_media_status(provider, token, handle):
    target = str(provider.credentials.get("ig_user_id") or "")
    if not native_id(target) or not native_id(handle):
        return PublishStatus(PublishState.FAILED, error="Instagram media receipt or bound account is invalid.")
    host = "https://graph.facebook.com/v25.0"
    row = native_read(provider, f"{host}/{handle}", token, {"fields": "id,owner{id},permalink,media_type"})
    if not isinstance(row, dict) or str(row.get("id") or "") != handle:
        return pending("Instagram did not return the exact published-media receipt.")
    owner = row.get("owner")
    if owner is not None and owner != {}:
        if not isinstance(owner, dict) or not native_id(str(owner.get("id") or "")):
            return pending("Instagram media owner metadata is malformed.")
        if str(owner["id"]) != target:
            return PublishStatus(PublishState.FAILED, error="Instagram media belongs to a different account.")
    else:
        # One bounded canonical edge read proves membership when Meta omits
        # owner. Absence on this page is unknown, never proof of failure.
        page = native_read(provider, f"{host}/{target}/media", token, {"fields": "id", "limit": 50})
        rows = page.get("data") if isinstance(page, dict) else None
        if (
            not isinstance(rows, list)
            or len(rows) > 50
            or not any(isinstance(x, dict) and str(x.get("id") or "") == handle for x in rows)
        ):
            return pending("Instagram media ownership is not confirmed.")
    link = permalink(row.get("permalink"), facebook=False)
    if row.get("media_type") not in {"IMAGE", "VIDEO", "CAROUSEL_ALBUM"} or not link:
        return pending("Instagram published-media type or permalink is unavailable.")
    return PublishStatus(
        PublishState.COMPLETE, platform_post_id=handle, raw={"id": handle, "owner_id": target, "permalink": link}
    )
