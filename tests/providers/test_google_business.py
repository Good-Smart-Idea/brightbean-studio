"""Google Business Profile requests match the documented HTTP API.

Connect lists locations through ``get_profile``. ``accounts.locations.list``
requires ``readMask``; omitting it is a 400. v4 ``localPosts.create`` takes
``accounts/{account}/locations/{location}/localPosts``. The Business
Information API returns location names as ``locations/{locationId}``, so
publishing at ``v4/{location name}/localPosts`` never reaches a location.
"""

import json

import httpx
import pytest

from providers.google_business import GoogleBusinessProvider
from providers.types import PublishContent

READ_MASK = "name,title,storefrontAddress,phoneNumbers"
LOCAL_POSTS_PATH = "/v4/accounts/123/locations/456/localPosts"
LOCAL_POST_NAME = "accounts/123/locations/456/localPosts/789"


def _install(monkeypatch, responses: list[dict]) -> list[httpx.Request]:
    """Route the provider's httpx client at a scripted transport and record calls."""
    pending = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if not pending:
            raise AssertionError(f"unexpected {request.method} {request.url}")
        return httpx.Response(200, json=pending.pop(0))

    transport = httpx.MockTransport(handler)
    original = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return seen


def _provider(**credentials) -> GoogleBusinessProvider:
    base = {"client_id": "cid", "client_secret": "sec"}
    base.update(credentials)
    return GoogleBusinessProvider(base)


def _locations_request(seen: list[httpx.Request]) -> httpx.Request:
    matches = [request for request in seen if request.url.path.endswith("/locations")]
    assert len(matches) == 1
    return matches[0]


def test_get_profile_sends_read_mask_when_listing_locations(monkeypatch):
    """Connect calls get_profile, which lists locations. readMask is required."""
    seen = _install(
        monkeypatch,
        [
            {"accounts": [{"name": "accounts/123"}]},
            {
                "locations": [
                    {
                        "name": "locations/456",
                        "title": "Bright Bean Cafe",
                        "storefrontAddress": {"addressLines": ["1 Main St"]},
                        "phoneNumbers": {"primaryPhone": "555-0100"},
                    }
                ]
            },
        ],
    )

    profile = _provider().get_profile("ya29.token")

    assert profile.platform_id == "locations/456"
    assert profile.name == "Bright Bean Cafe"
    assert profile.extra == {"address": "1 Main St", "phone": "555-0100"}
    locations = _locations_request(seen)
    assert locations.method == "GET"
    assert locations.url.host == "mybusinessbusinessinformation.googleapis.com"
    assert locations.url.path == "/v1/accounts/123/locations"
    assert locations.url.params["readMask"] == READ_MASK


@pytest.mark.parametrize(
    "credentials",
    [
        pytest.param({}, id="discovered-location-name"),
        pytest.param(
            {"account_id": "accounts/123", "location_id": "locations/456"},
            id="credential-location-resource-name",
        ),
        pytest.param({"account_id": "123", "location_id": "456"}, id="credential-bare-ids"),
    ],
)
def test_publish_post_uses_account_qualified_local_posts_path(monkeypatch, credentials):
    """localPosts.create parent is accounts/{account}/locations/{location}."""
    responses: list[dict] = []
    if "account_id" not in credentials:
        responses.append({"accounts": [{"name": "accounts/123"}]})
    if "location_id" not in credentials:
        responses.append({"locations": [{"name": "locations/456"}]})
    responses.append(
        {
            "name": LOCAL_POST_NAME,
            "searchUrl": "https://local.google.com/place?id=789",
        }
    )
    seen = _install(monkeypatch, responses)

    result = _provider(**credentials).publish_post("ya29.token", PublishContent(text="Hello from the shop"))

    assert result.platform_post_id == LOCAL_POST_NAME
    created = [request for request in seen if request.method == "POST"]
    assert len(created) == 1
    assert created[0].url.host == "mybusiness.googleapis.com"
    assert created[0].url.path == LOCAL_POSTS_PATH
    assert json.loads(created[0].content)["summary"] == "Hello from the shop"
    if "location_id" not in credentials:
        locations = _locations_request(seen)
        assert locations.url.params["readMask"] == READ_MASK


def test_get_post_metrics_uses_account_qualified_local_post_name(monkeypatch):
    """localPosts.get is v4/accounts/{account}/locations/{location}/localPosts/{post}."""
    seen = _install(
        monkeypatch,
        [{"searchActionMetrics": [{"metricType": "QUERIES_DIRECT", "value": 4}]}],
    )

    metrics = _provider().get_post_metrics("ya29.token", LOCAL_POST_NAME)

    assert metrics.impressions == 4
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.host == "mybusiness.googleapis.com"
    assert seen[0].url.path == f"/v4/{LOCAL_POST_NAME}"
