"""Google Business Profile API provider implementation."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ACCOUNTS_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
POSTS_API = "https://mybusiness.googleapis.com/v4"

# accounts.locations.list rejects a call that omits readMask. These are the
# Location fields connect reads back; publish only needs ``name``.
LOCATION_READ_MASK = "name,title,storefrontAddress,phoneNumbers"
_LIST_PAGE_SIZE = 100
_MAX_LIST_PAGES = 20


def _resource_id(resource_name: str) -> str:
    """Last segment of a resource name, or the value when it is already an id."""
    return resource_name.strip("/").rsplit("/", 1)[-1]


def _v4_location_parent(account_id: str, location_id: str) -> str:
    """Parent for v4 localPosts methods: ``accounts/{account}/locations/{location}``.

    Business Information location names are ``locations/{locationId}`` and do
    not include the account. A credential may be that name, a bare id, or the
    full account-qualified parent.
    """
    location = location_id.strip("/")
    if location.startswith("accounts/") and "/locations/" in location:
        account_part, _, after = location.partition("/locations/")
        location_part = after.split("/", 1)[0]
        return f"{account_part}/locations/{location_part}"
    return f"accounts/{_resource_id(account_id)}/locations/{_resource_id(location)}"


def stored_google_business_target(platform_id: str) -> tuple[str, str]:
    """Split a stored id into ``(accounts/{account}, locations/{location})``.

    Either side is ``""`` when the id does not carry it. New connections store
    ``accounts/{account}/locations/{location}``. Connections made before a
    location could be chosen stored the Business Information name
    ``locations/{location}`` and no account.
    """
    raw = (platform_id or "").strip().strip("/")
    if raw.startswith("accounts/") and "/locations/" in raw:
        account_part, _, after = raw.partition("/locations/")
        location_part = after.split("/", 1)[0]
        account = account_part if account_part.startswith("accounts/") else ""
        location = f"locations/{location_part}" if location_part else ""
        return account, location
    if raw.startswith("locations/"):
        location_part = raw.removeprefix("locations/").split("/", 1)[0]
        return "", f"locations/{location_part}" if location_part else ""
    if raw.startswith("accounts/") and "/" not in raw.removeprefix("accounts/"):
        return raw, ""
    return "", ""


def apply_stored_google_business_target(credentials: dict, platform_id: str) -> None:
    """Point this provider at one social account's location.

    An org credential may pin a single ``location_id`` for every Google
    Business connection in the org. The connected account's stored names win.
    A legacy row that only has ``locations/{id}`` still publishes to that
    location; the account name falls back to the org pin (including an
    account embedded in a full ``accounts/{a}/locations/{l}`` pin) and, when
    that is missing too, to the first account the provider can list.
    """
    stored_account, stored_location = stored_google_business_target(platform_id)
    pinned_account, pinned_location = stored_google_business_target(str(credentials.get("location_id") or ""))
    explicit_account, _ = stored_google_business_target(str(credentials.get("account_id") or ""))
    if not explicit_account:
        bare_account = str(credentials.get("account_id") or "").strip().strip("/")
        if bare_account and "/" not in bare_account:
            explicit_account = f"accounts/{bare_account}"
    account_name = stored_account or explicit_account or pinned_account
    location_name = stored_location or pinned_location
    if account_name:
        credentials["account_id"] = account_name
    if location_name:
        credentials["location_id"] = location_name


def _location_address(location: dict) -> str:
    lines = (location.get("storefrontAddress") or {}).get("addressLines") or []
    return ", ".join(line for line in lines if line)


def _pick_location(locations: list[dict], wanted: str | None) -> dict | None:
    """The location ``wanted`` names, or the first when nothing was stored.

    A stored location that is not in the list must not fall through to the
    first row — that is how every connection in an org was published to one
    place.
    """
    if not locations:
        return None
    if not wanted:
        return locations[0]
    _, wanted_location = stored_google_business_target(wanted)
    wanted_id = _resource_id(wanted_location or wanted)
    for location in locations:
        if _resource_id(location.get("name") or "") == wanted_id:
            return location
    return None


class GoogleBusinessProvider(SocialProvider):
    """Google Business Profile provider.

    Uses Google OAuth 2.0.  The ``credentials`` dict must contain:

    - ``client_id``
    - ``client_secret``

    Optional, and set per connected location at publish time:

    - ``account_id`` – ``accounts/{accountId}``
    - ``location_id`` – ``locations/{locationId}`` or the v4 parent
      ``accounts/{accountId}/locations/{locationId}``
    """

    # Publishes from hosted URLs only — the platform fetches the media
    # itself, so ``PublishContent.media_files`` is never read and the engine
    # can skip downloading the asset to local disk entirely.
    needs_local_media = False

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Google Business"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 1500

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG]

    @property
    def required_scopes(self) -> list[str]:
        return ["https://www.googleapis.com/auth/business.manage"]

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        """Build the Google OAuth 2.0 authorization URL."""
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.required_scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        """Exchange an authorization code for Google access/refresh tokens."""
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        data = resp.json()
        if "error" in data:
            raise OAuthError(
                f"Token exchange failed: {data.get('error_description', data['error'])}",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired Google access token."""
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()
        if "error" in data:
            raise OAuthError(
                f"Token refresh failed: {data.get('error_description', data['error'])}",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=refresh_token,  # Google doesn't rotate refresh tokens
            expires_in=data.get("expires_in"),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def revoke_token(self, access_token: str) -> bool:
        """Revoke a Google OAuth token."""
        try:
            self._request(
                "POST",
                REVOKE_URL,
                params={"token": access_token},
            )
            return True
        except Exception:
            logger.exception("Failed to revoke Google token")
            return False

    # ------------------------------------------------------------------
    # Account / location helpers
    # ------------------------------------------------------------------

    def _get_account_id(self, access_token: str) -> str:
        """Return the account resource name stored for this connection, or the first listed."""
        if self.credentials.get("account_id"):
            return self.credentials["account_id"]

        names = self._list_account_names(access_token)
        if not names:
            raise PublishError(
                "No Google Business accounts found",
                platform=self.platform_name,
            )
        # Legacy connections did not store an account. The first one is the
        # same fallback publish used before locations could be chosen.
        return names[0]

    def _get_location_id(self, access_token: str, account_id: str) -> str:
        """Return the location stored for this connection, or the first listed."""
        if self.credentials.get("location_id"):
            return self.credentials["location_id"]

        locations = self._list_locations(access_token, account_id)
        if not locations:
            raise PublishError(
                "No locations found for Google Business account",
                platform=self.platform_name,
            )
        return locations[0]["name"]

    def _list_account_names(self, access_token: str) -> list[str]:
        """Every account the token can access. Ignores an org-level pin."""
        names: list[str] = []
        page_token = None
        for _ in range(_MAX_LIST_PAGES):
            params: dict = {"pageSize": _LIST_PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            resp = self._request(
                "GET",
                f"{ACCOUNTS_API}/accounts",
                access_token=access_token,
                params=params,
            )
            data = resp.json()
            names.extend(account["name"] for account in data.get("accounts") or [] if account.get("name"))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return names

    def _list_locations(self, access_token: str, account_id: str) -> list[dict]:
        """List locations. ``readMask`` is required by accounts.locations.list."""
        locations: list[dict] = []
        page_token = None
        for _ in range(_MAX_LIST_PAGES):
            params: dict = {"readMask": LOCATION_READ_MASK, "pageSize": _LIST_PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            resp = self._request(
                "GET",
                f"{BUSINESS_INFO_API}/{account_id}/locations",
                access_token=access_token,
                params=params,
            )
            data = resp.json()
            locations.extend(data.get("locations") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return locations

    def list_connectable_locations(self, access_token: str) -> list[dict]:
        """Locations this login can connect, one picker row per location.

        ``id`` is the v4 parent ``accounts/{account}/locations/{location}``.
        That string is both resource names, and it is what we store on the
        social account. Listing ignores an org-level account or location pin
        so two locations under one login can both be connected.
        """
        rows: list[dict] = []
        seen: set[str] = set()
        for account_name in self._list_account_names(access_token):
            for location in self._list_locations(access_token, account_name):
                location_name = location.get("name") or ""
                if not location_name:
                    continue
                parent = _v4_location_parent(account_name, location_name)
                if parent in seen:
                    continue
                seen.add(parent)
                rows.append(
                    {
                        "id": parent,
                        "name": location.get("title") or location_name,
                        "category": _location_address(location),
                        "account_id": stored_google_business_target(account_name)[0] or account_name,
                        "location_id": f"locations/{_resource_id(location_name)}",
                    }
                )
        return rows

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Fetch the connected location, or the first location when none is stored."""
        account_id = self._get_account_id(access_token)
        wanted = self.credentials.get("location_id") or None
        location = _pick_location(self._list_locations(access_token, account_id), wanted)
        if location is None:
            # A stored location that this account no longer lists must not be
            # replaced with a sibling's title.
            return AccountProfile(
                platform_id=str(wanted or account_id),
                name="" if wanted else str(account_id),
            )

        phone = (location.get("phoneNumbers") or {}).get("primaryPhone", "")
        return AccountProfile(
            platform_id=location.get("name") or str(wanted or account_id),
            name=location.get("title") or location.get("name") or "",
            handle=None,
            extra={"address": _location_address(location), "phone": phone},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Publish a local post to Google Business Profile."""
        if content.text and len(content.text) > self.max_caption_length:
            raise PublishError(
                f"Post text exceeds {self.max_caption_length} characters (got {len(content.text)})",
                platform=self.platform_name,
                retryable=False,
            )

        account_id = self._get_account_id(access_token)
        location_id = self._get_location_id(access_token, account_id)

        # Determine topic type
        topic_type = content.extra.get("topic_type", "STANDARD")

        body: dict = {
            "languageCode": content.extra.get("language_code", "en"),
            "summary": content.text or "",
            "topicType": topic_type,
        }

        # Attach media
        if content.media_urls:
            body["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": url} for url in content.media_urls]

        # EVENT type extras
        if topic_type == "EVENT" and content.extra.get("event"):
            body["event"] = content.extra["event"]

        # OFFER type extras
        if topic_type == "OFFER" and content.extra.get("offer"):
            body["offer"] = content.extra["offer"]

        resp = self._request(
            "POST",
            f"{POSTS_API}/{_v4_location_parent(account_id, location_id)}/localPosts",
            access_token=access_token,
            json=body,
        )
        data = resp.json()

        post_name = data.get("name", "")
        return PublishResult(
            platform_post_id=post_name,
            url=data.get("searchUrl"),
            extra=data,
        )

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        """Fetch metrics for a Google Business local post.

        A LocalPost ``name`` stored at publish time is already
        ``accounts/{account}/locations/{location}/localPosts/{post}``. A bare
        id is qualified with this connection's stored location, not whichever
        location the org credential or the first list row would pick.
        """
        resp = self._request(
            "GET",
            f"{POSTS_API}/{self._local_post_name(access_token, post_id)}",
            access_token=access_token,
        )
        data = resp.json()
        search_views = 0
        maps_views = 0
        for metric in data.get("searchActionMetrics", []):
            if metric.get("metricType") == "QUERIES_DIRECT":
                search_views += metric.get("value", 0)
        return PostMetrics(
            impressions=search_views + maps_views,
            extra={
                "search_views": search_views,
                "maps_views": maps_views,
                "raw": data,
            },
        )

    def _local_post_name(self, access_token: str, post_id: str) -> str:
        name = (post_id or "").strip().strip("/")
        if name.startswith("accounts/") and "/localPosts/" in name:
            return name
        account_id = self._get_account_id(access_token)
        location_id = self._get_location_id(access_token, account_id)
        local_id = name.rsplit("/", 1)[-1] if name else ""
        return f"{_v4_location_parent(account_id, location_id)}/localPosts/{local_id}"
