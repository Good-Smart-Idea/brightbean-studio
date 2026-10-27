"""Native media reconciliation remains scoped, read-only, bounded, and redacted."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.test import Client
from django.utils import timezone

from apps.api import recent_media as adapter
from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def setup(db, user, organization):
    ws = Workspace.objects.create(name="Native inventory", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=ws, workspace_role="owner")
    ig = SocialAccount.objects.create(
        workspace=ws, platform="instagram", account_platform_id="123", account_name="IG", oauth_access_token="test-only"
    )
    fb = SocialAccount.objects.create(
        workspace=ws, platform="facebook", account_platform_id="456", account_name="FB", oauth_access_token="test-only"
    )
    key = services.issue_api_key(
        workspace=ws, social_accounts=[ig, fb], issued_by=user, name="native", permissions=list(PERMISSION_KEYS)
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    return SimpleNamespace(ws=ws, user=user, ig=ig, fb=fb, client=client)


def get(setup, account=None, suffix="recent-media"):
    return setup.client.get(f"/api/v1/accounts/{(account or setup.ig).id}/{suffix}", secure=True)


def fake_provider(monkeypatch, *bodies):
    provider = Mock()
    provider._request.side_effect = [Mock(json=Mock(return_value=body)) for body in bodies]
    monkeypatch.setattr(adapter, "get_provider", Mock(return_value=provider))
    monkeypatch.setattr(adapter, "_resolve_publish_credentials", Mock(return_value={}))
    return provider


def ig_item():
    return {
        "id": "789",
        "caption": "HVAC",
        "timestamp": "2026-10-27T14:58:00+0000",
        "media_type": "VIDEO",
        "media_product_type": "REELS",
        "permalink": "https://www.instagram.com/reel/abc/?secret=never-return",
        "access_token": "<REPLACE_WITH_SECRET>",
        "media_url": "https://private.example/never-return",
    }


@pytest.mark.django_db
def test_native_ig_mapping_and_get_only(setup, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "123", "username": "owner"}, {"data": [ig_item()]})
    response = get(setup)
    assert response.status_code == 200
    body = response.json()
    assert body["destination"]["id"] == "123"
    assert body["items"][0]["media_product_type"] == "REELS"
    assert body["items"][0]["permalink"] == "https://www.instagram.com/reel/abc/"
    assert body["complete_inventory"] is True
    assert "never-return" not in response.content.decode()
    assert "<REPLACE_WITH_SECRET>" not in response.content.decode()
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)
    assert provider._request.call_args.args[1].endswith("/123/media")
    assert Post.objects.count() == 0


@pytest.mark.django_db
def test_cursor_metadata_and_no_next_url_following(setup, monkeypatch):
    provider = fake_provider(
        monkeypatch,
        {"id": "123"},
        {"data": [ig_item()], "paging": {"next": "https://untrusted/secret", "cursors": {"after": "safe-cursor"}}},
    )
    body = get(setup).json()
    assert body["has_more"] is True and body["next_cursor"] == "safe-cursor"
    assert body["complete_inventory"] is False
    assert body["oldest_timestamp"] == body["newest_timestamp"]
    assert provider._request.call_count == 2
    assert "untrusted" not in str(body)


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["limit=0", "limit=51", "after=bad%20cursor"])
def test_input_bounds_before_provider(setup, monkeypatch, query):
    provider = fake_provider(monkeypatch)
    assert get(setup, suffix="recent-media?" + query).status_code == 422
    provider._request.assert_not_called()


@pytest.mark.django_db
def test_auth_and_allowlist_denied_before_provider(setup, monkeypatch):
    provider = fake_provider(monkeypatch)
    assert Client().get(f"/api/v1/accounts/{setup.ig.id}/recent-media", secure=True).status_code == 401
    other = SocialAccount.objects.create(workspace=setup.ws, platform="instagram", account_platform_id="999")
    assert get(setup, other).status_code == 403
    provider._request.assert_not_called()


@pytest.mark.django_db
def test_missing_permission_denied(setup, monkeypatch):
    provider = fake_provider(monkeypatch)
    key = services.issue_api_key(
        workspace=setup.ws,
        social_accounts=[setup.ig],
        issued_by=setup.user,
        name="limited",
        permissions=["create_posts"],
    )
    setup.client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    assert get(setup).status_code == 403
    provider._request.assert_not_called()


@pytest.mark.django_db
def test_unsupported_platform_before_provider(setup, monkeypatch):
    provider = fake_provider(monkeypatch)
    setup.ig.platform = "youtube"
    setup.ig.save(update_fields=["platform"])
    assert get(setup).status_code == 422
    provider._request.assert_not_called()


@pytest.mark.django_db
def test_provider_errors_redacted_no_state_change(setup, monkeypatch):
    provider = fake_provider(monkeypatch)
    provider._request.side_effect = RuntimeError("access_token=never-return secret")
    response = get(setup)
    assert response.status_code == 502
    assert "never-return" not in response.content.decode()
    assert "<REPLACE_WITH_SECRET>" not in response.content.decode()
    setup.ig.refresh_from_db()
    assert setup.ig.oauth_access_token == "test-only"
    assert Post.objects.count() == 0


@pytest.mark.django_db
def test_wrong_native_identity_fails_closed(setup, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "999"})
    assert get(setup).status_code == 502
    assert provider._request.call_count == 1


@pytest.mark.django_db
def test_facebook_video_page_maps_destination_status(setup, monkeypatch):
    provider = fake_provider(
        monkeypatch,
        {"id": "456", "name": "GSI", "link": "https://www.facebook.com/gsi"},
        {
            "data": [
                {
                    "id": "987",
                    "created_time": "2026-10-27T14:58:00+0000",
                    "description": "HVAC",
                    "published": False,
                    "status": {"video_status": "processing", "secret": "never-return"},
                    "permalink_url": "/gsi/videos/987/",
                }
            ]
        },
    )
    body = get(setup, setup.fb).json()
    assert body["destination"]["name"] == "GSI"
    assert body["items"][0]["published"] is False
    assert body["items"][0]["video_status"] == "processing"
    assert body["items"][0]["permalink"] == "https://www.facebook.com/gsi/videos/987/"
    assert provider._request.call_args.args[1].endswith("/456/videos")


@pytest.fixture
def fb_post(setup):
    post = Post.objects.create(workspace=setup.ws, author=setup.user, caption="HVAC")
    return PlatformPost.objects.create(
        post=post, social_account=setup.fb, platform_post_id="987", status="published", published_at=timezone.now()
    )


@pytest.mark.django_db
def test_exact_fb_receipt_bound_to_account_no_visibility_inference(setup, fb_post, monkeypatch):
    provider = fake_provider(
        monkeypatch,
        {"id": "456", "name": "GSI"},
        {
            "id": "987",
            "from": {"id": "456"},
            "published": True,
            "status": {"video_status": "ready"},
            "privacy": {"value": "EVERYONE"},
            "permalink_url": "/gsi/videos/987/",
            "access_token": "<REPLACE_WITH_SECRET>",
        },
    )
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["published"] is True and body["owner_id"] == "456"
    assert body["privacy"] == "EVERYONE"
    assert body["public_visibility_verified"] is False
    assert "never-return" not in str(body)
    assert "<REPLACE_WITH_SECRET>" not in str(body)
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)
    fb_post.refresh_from_db()
    assert fb_post.status == "published" and fb_post.platform_post_id == "987"


@pytest.mark.django_db
def test_exact_fb_wrong_local_account_denied(setup, fb_post, monkeypatch):
    provider = fake_provider(monkeypatch)
    fb_post.social_account = setup.ig
    fb_post.save(update_fields=["social_account"])
    assert get(setup, setup.fb, f"published-media/{fb_post.id}").status_code == 404
    provider._request.assert_not_called()


@pytest.mark.django_db
def test_exact_fb_wrong_native_owner_redacted(setup, fb_post, monkeypatch):
    fake_provider(monkeypatch, {"id": "456"}, {"id": "987", "from": {"id": "999", "name": "never-return"}})
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 502
    assert "never-return" not in response.content.decode()
    assert "<REPLACE_WITH_SECRET>" not in response.content.decode()


@pytest.mark.django_db
def test_final_cursor_page_is_not_complete_inventory(setup, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "123"}, {"data": []})
    body = get(setup, suffix="recent-media?after=previous-page").json()
    assert body["has_more"] is False and body["complete_inventory"] is False
    assert provider._request.call_args.kwargs["params"]["after"] == "previous-page"
