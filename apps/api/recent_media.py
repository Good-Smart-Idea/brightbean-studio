"""Read-only native media inventory; no refresh, mutations, or provider next-URL following."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.utils.dateparse import parse_datetime
from ninja import Field, Schema

from apps.publisher.engine import _resolve_publish_credentials
from providers import get_provider
from providers.exceptions import APIError

FIELDS = "id,caption,timestamp,media_type,media_product_type,permalink"
HOSTS = {"instagram": "https://graph.facebook.com/v25.0", "instagram_login": "https://graph.instagram.com/v25.0"}
FACEBOOK_HOST = "https://graph.facebook.com/v25.0"
FACEBOOK_VIDEO_FIELDS = "id,description,created_time,permalink_url,published,status,privacy,from"
FACEBOOK_POST_FIELDS = "id,object_id,permalink_url,is_published,is_hidden,timeline_visibility,privacy,from"


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
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    hosts = ("www.facebook.com", "facebook.com") if facebook else ("www.instagram.com", "instagram.com")
    if facebook and value.startswith("/") and not parsed.netloc:
        parsed = parsed._replace(scheme="https", netloc="www.facebook.com")
    if parsed.scheme != "https" or parsed.netloc not in hosts:
        return ""
    # Facebook's watch/permalink forms need these public object IDs. Keep
    # only numeric values; credentials, tracking and arbitrary query keys go.
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query)
            if facebook and key in ("v", "id", "story_fbid") and value.isascii() and value.isdecimal()
        ]
    )
    return urlunsplit(("https", parsed.netloc, parsed.path, query, ""))


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
    receipt_node_type: Literal["post", "video"] = "video"
    canonical_post_id: str = ""
    post_object_id: str = Field(
        "", description="Object reference from the verified Page Post; not proof of video ownership."
    )
    post_is_published: bool | None = None
    post_is_hidden: bool | None = None
    timeline_visibility: Literal["", "hidden", "normal", "forced_allow"] = ""
    post_privacy: str = ""
    video_id: str = ""
    video_lookup_status: Literal["available", "no_object_id", "unavailable"] = "available"
    published: bool | None = Field(
        None, description="Video-node published flag, independent of the Page Post's visibility."
    )
    video_status: str = ""
    privacy: str = ""
    permalink: str = ""
    # Provider metadata does not establish what a logged-out visitor sees.
    public_visibility_verified: bool = False


def _receipt_ids(remote_id, identifier):
    if not isinstance(remote_id, str):
        raise ValueError("No native receipt")
    parts = remote_id.split("_")
    if len(parts) == 2 and parts[0] == identifier:
        leaf = parts[1]
    elif len(parts) == 1:
        leaf = parts[0]
    else:
        raise ValueError("Receipt does not belong to scoped Page")
    if not leaf.isascii() or not leaf.isdecimal():
        raise ValueError("Invalid native receipt")
    return f"{identifier}_{leaf}", leaf


def _owned_node(row, expected_id, identifier):
    if not isinstance(row, dict) or str(row.get("id", "")) != expected_id:
        raise ValueError("Invalid native node")
    owner = row.get("from") or {}
    if not isinstance(owner, dict) or str(owner.get("id", "")) != identifier:
        raise ValueError("Native owner does not match scoped Page")
    return row


def _read_video(provider, token, identifier, video_id):
    row = provider._request(
        "GET", f"{FACEBOOK_HOST}/{video_id}", access_token=token, params={"fields": FACEBOOK_VIDEO_FIELDS}
    ).json()
    return _owned_node(row, video_id, identifier)


def _privacy(row):
    privacy = row.get("privacy") or {}
    value = privacy.get("value", "") if isinstance(privacy, dict) else ""
    return value[:64] if isinstance(value, str) else ""


def _invalid_post_node(exc):
    """Only a definitive missing/type Post node permits a bare-Video read.

    Never turn authentication, quota, transport, 5xx, malformed data, or a
    generic exception into a second request against a different node type.
    """
    raw = exc.raw_response if isinstance(exc.raw_response, dict) else {}
    error = raw.get("error") or {}
    error = error if isinstance(error, dict) else {}
    if error.get("code") in (10, 102, 190, 200):
        return False
    message = str(error.get("message", "")).lower()
    # Graph's combined "missing OR cannot load due to permissions" response
    # is ambiguous, not a definitive type/not-found answer.
    if any(marker in message for marker in ("permission", "access token", "session")):
        return False
    if exc.status_code == 404:
        return True
    if exc.status_code != 400 or error.get("code") != 100:
        return False
    return any(
        marker in message
        for marker in (
            "unsupported get request",
            "does not exist",
            "nonexisting field",
            "non-existing field",
            "unknown path components",
        )
    )


def _video_values(row):
    return dict(
        video_id=str(row["id"]),
        published=row.get("published") if isinstance(row.get("published"), bool) else None,
        video_status=_video_status(row),
        privacy=_privacy(row),
    )


def fetch_facebook_video(account, platform_post) -> NativeVideoResponse:
    """Read a stored Page Post receipt, following only its verified video ID.

    Facebook._publish_video stores the stripped feed-Post ID when available,
    otherwise a bare Video ID. One Page identity read plus at most two node
    reads settles that distinction without a write, history rewrite or retry.
    """
    provider, token, identifier = _client(account)
    remote_id = platform_post.platform_post_id
    canonical_id, leaf = _receipt_ids(remote_id, identifier)
    destination = _destination(provider, token, identifier, platform="facebook")
    base = dict(
        account_id=account.id,
        platform_post_id=platform_post.id,
        destination=destination,
        remote_id=remote_id,
        owner_id=identifier,
    )
    try:
        post = provider._request(
            "GET", f"{FACEBOOK_HOST}/{canonical_id}", access_token=token, params={"fields": FACEBOOK_POST_FIELDS}
        ).json()
    except APIError as exc:
        if not _invalid_post_node(exc):
            raise
        video = _read_video(provider, token, identifier, leaf)
        return NativeVideoResponse(
            **base,
            **_video_values(video),
            receipt_node_type="video",
            permalink=_permalink(video.get("permalink_url", ""), facebook=True),
        )

    # Verify the Page-owned Post before trusting its object_id. A foreign
    # owner or malformed 2xx is not a reason to try a different native node.
    post = _owned_node(post, canonical_id, identifier)
    visibility = post.get("timeline_visibility", "")
    result = NativeVideoResponse(
        **base,
        receipt_node_type="post",
        canonical_post_id=canonical_id,
        post_is_published=post.get("is_published") if isinstance(post.get("is_published"), bool) else None,
        post_is_hidden=post.get("is_hidden") if isinstance(post.get("is_hidden"), bool) else None,
        timeline_visibility=visibility if visibility in ("hidden", "normal", "forced_allow") else "",
        post_privacy=_privacy(post),
        permalink=_permalink(post.get("permalink_url", ""), facebook=True)
        or f"https://www.facebook.com/{canonical_id}",
        video_lookup_status="no_object_id",
    )
    video_id = str(post.get("object_id") or "")
    if not video_id:
        return result
    if not video_id.isascii() or not video_id.isdecimal():
        raise ValueError("Invalid native Post object reference")
    result.post_object_id = video_id
    try:
        video = _read_video(provider, token, identifier, video_id)
    except APIError:
        # The verified Post is useful evidence even if its Video cannot be
        # read. This is explicitly partial, with no alternate ID request or
        # claim that the video is absent or publicly visible.
        result.video_lookup_status = "unavailable"
        return result
    for key, value in _video_values(video).items():
        setattr(result, key, value)
    result.video_lookup_status = "available"
    return result
