"""Read-only native media inventory; no refresh, mutations, or provider next-URL following."""

from __future__ import annotations

import datetime as dt
import uuid
from urllib.parse import urlsplit, urlunsplit

from django.utils.dateparse import parse_datetime
from ninja import Schema

from apps.publisher.engine import _resolve_publish_credentials
from providers import get_provider

FIELDS = "id,caption,timestamp,media_type,media_product_type,permalink"
HOSTS = {"instagram": "https://graph.facebook.com/v25.0", "instagram_login": "https://graph.instagram.com/v25.0"}
FACEBOOK_HOST = "https://graph.facebook.com/v25.0"
FACEBOOK_VIDEO_FIELDS = "id,description,created_time,permalink_url,published,status,privacy,from"


class NativeDestination(Schema):
    id: str
    name: str = ""
    handle: str = ""
    permalink: str = ""


class RecentMediaItem(Schema):
    id: str
    caption: str
    timestamp: dt.datetime
    media_type: str
    media_product_type: str = ""
    permalink: str = ""
    published: bool | None = None
    video_status: str = ""


class RecentMediaResponse(Schema):
    account_id: uuid.UUID
    destination: NativeDestination
    items: list[RecentMediaItem]
    limit: int
    has_more: bool
    next_cursor: str | None = None
    oldest_timestamp: dt.datetime | None = None
    newest_timestamp: dt.datetime | None = None
    complete_inventory: bool = False


def valid_cursor(value: str | None) -> bool:
    return value is None or (0 < len(value) <= 2048 and not any(c.isspace() for c in value))


def _permalink(value, *, facebook=False) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    hosts = ("www.facebook.com", "facebook.com") if facebook else ("www.instagram.com", "instagram.com")
    if facebook and value.startswith("/"):
        return "https://www.facebook.com" + parsed.path
    if parsed.scheme != "https" or parsed.netloc not in hosts:
        return ""
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _client(account):
    identifier = account.account_platform_id
    if not isinstance(identifier, str) or not identifier.isdecimal():
        raise ValueError("Account has no valid native identifier")
    token = account.oauth_access_token
    if not token:
        raise ValueError("Account has no usable credential")
    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    return provider, token, identifier


def _destination(provider, token, identifier, *, platform):
    facebook = platform == "facebook"
    host = FACEBOOK_HOST if facebook else HOSTS[platform]
    fields = "id,name,link" if facebook else "id,username,name"
    raw = provider._request("GET", f"{host}/{identifier}", access_token=token, params={"fields": fields}).json()
    if not isinstance(raw, dict) or str(raw.get("id", "")) != identifier:
        raise ValueError("Native destination does not match scoped account")
    return NativeDestination(
        id=identifier,
        name=str(raw.get("name", "")),
        handle=str(raw.get("username", "")),
        permalink=_permalink(raw.get("link", ""), facebook=facebook),
    )


def _video_status(row):
    status = row.get("status")
    if not isinstance(status, dict):
        return ""
    value = status.get("video_status")
    return value[:64] if isinstance(value, str) else ""


def fetch_recent_media(account, *, limit: int, after: str | None) -> RecentMediaResponse:
    """One GET to the explicitly scoped account's native /media edge.

    A page proves presence only. Follow cursors until the target time window
    is covered before concluding absence. No token refresh is attempted.
    """
    provider, token, identifier = _client(account)
    facebook = account.platform == "facebook"
    destination = _destination(provider, token, identifier, platform=account.platform)
    host = FACEBOOK_HOST if facebook else HOSTS[account.platform]
    edge = "videos" if facebook else "media"
    params = {"fields": FACEBOOK_VIDEO_FIELDS if facebook else FIELDS, "limit": limit}
    if after:
        params["after"] = after
    response = provider._request("GET", f"{host}/{identifier}/{edge}", access_token=token, params=params)
    body = response.json()
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("Invalid media page")
    items = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("id", "")).isdecimal():
            raise ValueError("Invalid media item")
        stamp = parse_datetime(str(row.get("created_time" if facebook else "timestamp", "")))
        if stamp is None or stamp.tzinfo is None:
            raise ValueError("Invalid media timestamp")
        items.append(
            RecentMediaItem(
                id=str(row["id"]),
                caption=str(row.get("description" if facebook else "caption", "")),
                timestamp=stamp,
                media_type="VIDEO" if facebook else str(row.get("media_type", "")),
                media_product_type=str(row.get("media_product_type", "")),
                permalink=_permalink(row.get("permalink_url" if facebook else "permalink", ""), facebook=facebook),
                published=row.get("published") if isinstance(row.get("published"), bool) else None,
                video_status=_video_status(row) if facebook else "",
            )
        )
    paging = body.get("paging") or {}
    has_more = bool(paging.get("next"))
    cursor = (paging.get("cursors") or {}).get("after") if has_more else None
    if has_more and (not isinstance(cursor, str) or not valid_cursor(cursor) or cursor == after):
        raise ValueError("Invalid media pagination")
    stamps = [item.timestamp for item in items]
    return RecentMediaResponse(
        account_id=account.id,
        destination=destination,
        items=items,
        limit=limit,
        has_more=has_more,
        next_cursor=cursor,
        oldest_timestamp=min(stamps) if stamps else None,
        newest_timestamp=max(stamps) if stamps else None,
        complete_inventory=not has_more and after is None,
    )


class NativeVideoResponse(Schema):
    account_id: uuid.UUID
    platform_post_id: uuid.UUID
    destination: NativeDestination
    remote_id: str
    owner_id: str
    published: bool | None = None
    video_status: str = ""
    privacy: str = ""
    permalink: str = ""
    # Provider metadata does not establish what a logged-out visitor sees.
    public_visibility_verified: bool = False


def fetch_facebook_video(account, platform_post) -> NativeVideoResponse:
    provider, token, identifier = _client(account)
    remote_id = platform_post.platform_post_id
    if not isinstance(remote_id, str) or not remote_id.isdecimal():
        raise ValueError("No native video receipt")
    destination = _destination(provider, token, identifier, platform="facebook")
    row = provider._request(
        "GET", f"{FACEBOOK_HOST}/{remote_id}", access_token=token, params={"fields": FACEBOOK_VIDEO_FIELDS}
    ).json()
    if not isinstance(row, dict) or str(row.get("id", "")) != remote_id:
        raise ValueError("Invalid native video")
    owner = row.get("from") or {}
    if not isinstance(owner, dict) or str(owner.get("id", "")) != identifier:
        raise ValueError("Video owner does not match scoped account")
    privacy = row.get("privacy") or {}
    value = privacy.get("value", "") if isinstance(privacy, dict) else ""
    return NativeVideoResponse(
        account_id=account.id,
        platform_post_id=platform_post.id,
        destination=destination,
        remote_id=remote_id,
        owner_id=identifier,
        published=row.get("published") if isinstance(row.get("published"), bool) else None,
        video_status=_video_status(row),
        privacy=value[:64] if isinstance(value, str) else "",
        permalink=_permalink(row.get("permalink_url", ""), facebook=True),
    )
