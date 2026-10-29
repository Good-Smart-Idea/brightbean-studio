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
from providers.exceptions import APIError


@pytest.fixture
def setup(db, user, organization):
    ws = Workspace.objects.create(name="Native inventory", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=ws, workspace_role="owner")
    ig = SocialAccount.objects.create(
        workspace=ws,
        platform="instagram",
        account_platform_id="123",
        account_name="IG",
        oauth_access_token="test-only",
    )
    fb = SocialAccount.objects.create(
        workspace=ws,
        platform="facebook",
        account_platform_id="456",
        account_name="FB",
        oauth_access_token="test-only",
    )
    key = services.issue_api_key(
        workspace=ws,
        social_accounts=[ig, fb],
        issued_by=user,
        name="native",
        permissions=list(PERMISSION_KEYS),
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    return SimpleNamespace(ws=ws, user=user, ig=ig, fb=fb, client=client)


def get(setup, account=None, suffix="recent-media"):
    return setup.client.get(f"/api/v1/accounts/{(account or setup.ig).id}/{suffix}", secure=True)


def fake_provider(monkeypatch, *bodies):
    provider = Mock()
    provider._request.side_effect = [
        body if isinstance(body, Exception) else Mock(json=Mock(return_value=body)) for body in bodies
    ]
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
        {
            "data": [ig_item()],
            "paging": {
                "next": "https://untrusted/secret",
                "cursors": {"after": "safe-cursor"},
            },
        },
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
        post=post,
        social_account=setup.fb,
        platform_post_id="987",
        status="published",
        published_at=timezone.now(),
    )


@pytest.mark.django_db
def test_exact_fb_receipt_bound_to_account_no_visibility_inference(setup, fb_post, monkeypatch):
    provider = fake_provider(
        monkeypatch,
        {"id": "456", "name": "GSI"},
        {
            "id": "456_987",
            "object_id": "654",
            "from": {"id": "456"},
            "is_published": False,
            "is_hidden": True,
            "timeline_visibility": "hidden",
            "privacy": {"value": "EVERYONE"},
            "permalink_url": "/permalink.php?story_fbid=987&id=456&access_token=<REPLACE_WITH_SECRET>",
        },
        {
            "id": "654",
            "from": {"id": "456"},
            "published": True,
            "status": {"video_status": "ready"},
            "privacy": {"value": "EVERYONE"},
            "permalink_url": "/watch/?v=654&access_token=<REPLACE_WITH_SECRET>",
            "access_token": "<REPLACE_WITH_SECRET>",
        },
    )
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["published"] is True and body["owner_id"] == "456"
    assert body["receipt_node_type"] == "post" and body["canonical_post_id"] == "456_987"
    assert body["post_is_published"] is False and body["post_is_hidden"] is True
    assert body["timeline_visibility"] == "hidden"
    assert body["video_id"] == "654" and body["video_lookup_status"] == "available"
    assert body["permalink"] == "https://www.facebook.com/permalink.php?story_fbid=987&id=456"
    assert body["privacy"] == "EVERYONE"
    assert body["public_visibility_verified"] is False
    assert "never-return" not in str(body)
    assert "<REPLACE_WITH_SECRET>" not in str(body)
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)
    assert provider._request.call_count == 3
    assert provider._request.call_args_list[1].args[1].endswith("/456_987")
    assert provider._request.call_args_list[2].args[1].endswith("/654")
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
    provider = fake_provider(
        monkeypatch,
        {"id": "456"},
        {
            "id": "456_987",
            "object_id": "654",
            "from": {"id": "999", "name": "never-return"},
        },
    )
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 502
    assert "never-return" not in response.content.decode()
    assert "<REPLACE_WITH_SECRET>" not in response.content.decode()
    assert provider._request.call_count == 2  # Do not follow a foreign Post's object_id.


@pytest.mark.django_db
def test_final_cursor_page_is_not_complete_inventory(setup, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "123"}, {"data": []})
    body = get(setup, suffix="recent-media?after=previous-page").json()
    assert body["has_more"] is False and body["complete_inventory"] is False
    assert provider._request.call_args.kwargs["params"]["after"] == "previous-page"


def page_post(**changes):
    return {
        "id": "456_987",
        "object_id": "654",
        "from": {"id": "456"},
        "is_published": True,
        "is_hidden": False,
        "timeline_visibility": "normal",
        "permalink_url": "/permalink.php?story_fbid=987&id=456",
    } | changes


def video_node(**changes):
    return {
        "id": "654",
        "from": {"id": "456"},
        "published": False,
        "status": {"video_status": "processing"},
        "permalink_url": "/watch/?v=654",
    } | changes


@pytest.mark.django_db
@pytest.mark.parametrize(
    "upstream_error",
    [
        APIError("redacted", status_code=404),
        APIError(
            "redacted",
            status_code=400,
            raw_response={
                "error": {
                    "code": 100,
                    "message": "Tried accessing nonexisting field (object_id) on node type (Video)",
                }
            },
        ),
    ],
)
def test_bare_video_only_after_definitive_missing_or_type_post(setup, fb_post, monkeypatch, upstream_error):
    provider = fake_provider(monkeypatch, {"id": "456"}, upstream_error, video_node(id="987"))
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["receipt_node_type"] == "video" and body["video_id"] == "987"
    assert body["canonical_post_id"] == "" and body["post_is_published"] is None
    assert body["public_visibility_verified"] is False
    assert provider._request.call_count == 3
    assert provider._request.call_args_list[1].args[1].endswith("/456_987")
    assert provider._request.call_args_list[2].args[1].endswith("/987")
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)
    fb_post.refresh_from_db()
    assert fb_post.platform_post_id == "987" and fb_post.status == "published"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,code,message",
    [
        (401, 190, "access token redacted"),
        (403, 200, "permission denied"),
        (429, 4, "rate limit"),
        (500, 1, "upstream failed"),
        (400, 190, "invalid access token"),
        (404, 200, "permission denied"),
        (400, 100, "Permissions error without a node type marker"),
        (400, 100, "Invalid parameter"),
    ],
)
def test_no_bare_video_probe_on_auth_quota_server_or_unrelated_error(
    setup, fb_post, monkeypatch, status, code, message
):
    error = APIError(
        "never-return",
        status_code=status,
        raw_response={"error": {"code": code, "message": message}},
    )
    provider = fake_provider(monkeypatch, {"id": "456"}, error)
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 502
    assert "never-return" not in response.content.decode()
    assert provider._request.call_count == 2


@pytest.mark.django_db
def test_no_alternate_native_probe_on_generic_exception(setup, fb_post, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "456"}, RuntimeError("never-return"))
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 502
    assert "never-return" not in response.content.decode()
    assert provider._request.call_count == 2


@pytest.mark.django_db
def test_verified_post_without_object_is_not_video_absence_or_public_proof(setup, fb_post, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "456"}, page_post(object_id=None))
    body = get(setup, setup.fb, f"published-media/{fb_post.id}").json()
    assert body["receipt_node_type"] == "post" and body["post_is_published"] is True
    assert body["post_is_hidden"] is False and body["timeline_visibility"] == "normal"
    assert body["video_lookup_status"] == "no_object_id" and body["video_id"] == ""
    assert body["published"] is None and body["public_visibility_verified"] is False
    assert provider._request.call_count == 2


@pytest.mark.django_db
def test_video_read_failure_keeps_explicitly_partial_verified_post_proof(setup, fb_post, monkeypatch):
    error = APIError("never-return", status_code=403, raw_response={"error": {"code": 200}})
    provider = fake_provider(monkeypatch, {"id": "456"}, page_post(), error)
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["post_is_published"] is True and body["post_object_id"] == "654"
    assert body["video_lookup_status"] == "unavailable" and body["video_id"] == ""
    assert body["published"] is None and body["video_status"] == ""
    assert body["public_visibility_verified"] is False
    assert "never-return" not in response.content.decode()
    assert provider._request.call_count == 3  # No alternative-ID request after refusal.


@pytest.mark.django_db
def test_foreign_video_owner_not_exposed_even_after_owned_post(setup, fb_post, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "456"}, page_post(), video_node(**{"from": {"id": "999"}}))
    assert get(setup, setup.fb, f"published-media/{fb_post.id}").status_code == 502
    assert provider._request.call_count == 3


@pytest.mark.django_db
def test_own_page_prefixed_receipt_is_not_double_prefixed(setup, fb_post, monkeypatch):
    fb_post.platform_post_id = "456_987"
    fb_post.save(update_fields=["platform_post_id"])
    provider = fake_provider(monkeypatch, {"id": "456"}, page_post(), video_node())
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 200
    assert provider._request.call_args_list[1].args[1].endswith("/456_987")
    assert response.json()["remote_id"] == "456_987"


@pytest.mark.django_db
def test_foreign_page_prefix_never_probed(setup, fb_post, monkeypatch):
    fb_post.platform_post_id = "999_987"
    fb_post.save(update_fields=["platform_post_id"])
    provider = fake_provider(monkeypatch)
    assert get(setup, setup.fb, f"published-media/{fb_post.id}").status_code == 502
    provider._request.assert_not_called()


@pytest.mark.django_db
def test_non_numeric_object_reference_never_followed(setup, fb_post, monkeypatch):
    provider = fake_provider(
        monkeypatch,
        {"id": "456"},
        page_post(object_id="https://foreign.example/never-return"),
    )
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 502
    assert "never-return" not in response.content.decode()
    assert provider._request.call_count == 2


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "/watch/?v=654&access_token=never-return&t=3",
            "https://www.facebook.com/watch/?v=654",
        ),
        (
            "https://facebook.com/permalink.php?story_fbid=987&id=456&access_token=never-return",
            "https://facebook.com/permalink.php?story_fbid=987&id=456",
        ),
        (
            "https://www.facebook.com/story.php?story_fbid=987&id=456&fbid=999&tracking=never-return",
            "https://www.facebook.com/story.php?story_fbid=987&id=456",
        ),
        (
            "https://www.facebook.com/watch/?v=never-return&id=456",
            "https://www.facebook.com/watch/?id=456",
        ),
        ("//foreign.example/watch/?v=654", ""),
        ("https://foreign.example/watch/?v=654", ""),
        ("https://user:never-return@www.facebook.com/watch/?v=654", ""),
    ],
)
def test_facebook_permalink_keeps_only_essential_numeric_ids(url, expected):
    assert adapter._permalink(url, facebook=True) == expected


def ambiguous_post_error():
    return APIError(
        "never-return",
        status_code=400,
        raw_response={
            "error": {
                "code": 100,
                "error_subcode": 33,
                "message": "Unsupported get request. Object does not exist or cannot load due to permissions",
            }
        },
    )


@pytest.mark.django_db
@pytest.mark.parametrize("owner,expected", [("456", 200), ("999", 502)])
def test_ambiguous_post_can_only_establish_owner_verified_video(setup, fb_post, monkeypatch, owner, expected):
    provider = fake_provider(
        monkeypatch,
        {"id": "456"},
        ambiguous_post_error(),
        video_node(id="987", **{"from": {"id": owner}}),
    )
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == expected
    assert provider._request.call_count == 3
    assert "never-return" not in response.content.decode()
    if expected == 200:
        assert response.json()["receipt_node_type"] == "video"
        assert response.json()["post_is_published"] is None
        assert response.json()["public_visibility_verified"] is False


@pytest.mark.django_db
def test_both_node_reads_unavailable_never_claim_absence(setup, fb_post, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "456"}, ambiguous_post_error(), ambiguous_post_error())
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 502
    assert provider._request.call_count == 3
    assert "never-return" not in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ["configuration", "receipt", "destination", "post", "video"])
def test_receipt_diagnostics_each_stage_preserves_history(setup, fb_post, monkeypatch, stage, caplog):
    failure = APIError(
        "never-return https://private.example/?access_token=never-return",
        status_code=403,
        raw_response={
            "error": {
                "code": 200,
                "error_subcode": 33,
                "message": "never-return",
                "fbtrace_id": "never-return",
            }
        },
    )
    if stage == "configuration":
        provider = fake_provider(monkeypatch)
        monkeypatch.setattr(adapter, "_client", Mock(side_effect=ValueError("never-return")))
        reason = "configuration_invalid"
    elif stage == "receipt":
        provider = fake_provider(monkeypatch)
        fb_post.platform_post_id = "foreign_never-return"
        fb_post.save(update_fields=["platform_post_id"])
        reason = "receipt_invalid"
    else:
        bodies = [] if stage == "destination" else [{"id": "456"}]
        if stage == "video":
            bodies.append(ambiguous_post_error())
        provider = fake_provider(monkeypatch, *bodies, failure)
        reason = "provider_permission"
    before = (fb_post.status, fb_post.platform_post_id, fb_post.published_at)
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 502
    body = response.json()
    assert body["stage"] == stage and body["reason"] == reason
    assert body["error"] == "native_receipt_unverified"
    if stage in ("destination", "post", "video"):
        assert body["provider_http_status"] == 403
        assert body["provider_code"] == 200 and body["provider_subcode"] == 33
    assert "never-return" not in response.content.decode() + caplog.text
    assert "private.example" not in response.content.decode() + caplog.text
    assert provider._request.call_count <= 3
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)
    fb_post.refresh_from_db()
    assert (fb_post.status, fb_post.platform_post_id, fb_post.published_at) == before


@pytest.mark.parametrize(
    "error,reason",
    [
        (APIError("never-return", status_code=401), "provider_authentication"),
        (
            APIError("never-return", status_code=400, raw_response={"error": {"code": 190}}),
            "provider_authentication",
        ),
        (APIError("never-return", status_code=403), "provider_permission"),
        (APIError("never-return", status_code=429), "provider_rate_limit"),
        (APIError("never-return", status_code=503), "provider_unavailable"),
        (
            APIError("never-return", status_code=400, raw_response={"error": {"code": 4}}),
            "provider_rate_limit",
        ),
        (ambiguous_post_error(), "node_unavailable_or_type"),
        (
            APIError("never-return", status_code=400, raw_response={"error": {"code": 100}}),
            "provider_request_rejected",
        ),
        (RuntimeError("never-return"), "transport_or_internal_error"),
        (ValueError("never-return"), "native_response_invalid"),
        (adapter.NativeOwnerMismatchError("never-return"), "owner_mismatch"),
    ],
)
def test_fixed_receipt_diagnostic_reasons(error, reason):
    failure = adapter._receipt_failure("post", error)
    assert failure.reason == reason
    assert "never-return" not in failure.model_dump_json()


@pytest.mark.parametrize("value", [True, False, "190", -1, 2147483648, 1.5, None, {}, []])
def test_receipt_provider_codes_are_strict_bounded_integers(value):
    exc = APIError(
        "never-return",
        status_code=value,
        raw_response={"error": {"code": value, "error_subcode": value, "message": "never-return"}},
    )
    failure = adapter._receipt_failure("post", exc)
    assert failure.provider_http_status is None
    assert failure.provider_code is None and failure.provider_subcode is None
    assert "never-return" not in failure.model_dump_json()


@pytest.mark.parametrize(
    "raw",
    [[], "never-return", {"error": []}, {"error": "never-return"}, {"error": None}],
)
def test_malformed_provider_error_body_does_not_leak_or_break_diagnostics(raw):
    failure = adapter._receipt_failure("post", APIError("never-return", status_code=400, raw_response=raw))
    assert failure.reason == "provider_request_rejected"
    assert failure.provider_code is None and failure.provider_subcode is None
    assert "never-return" not in failure.model_dump_json()


@pytest.mark.django_db
def test_owner_mismatch_diagnostic_keeps_scope_boundary(setup, fb_post, monkeypatch):
    provider = fake_provider(monkeypatch, {"id": "456"}, page_post(**{"from": {"id": "999"}}))
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == 502
    assert response.json()["stage"] == "post"
    assert response.json()["reason"] == "owner_mismatch"
    assert provider._request.call_count == 2


def test_diagnostic_schema_rejects_open_stages_and_reasons():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        adapter.NativeReceiptFailure(stage="never-return", reason="provider_permission")
    with pytest.raises(ValidationError):
        adapter.NativeReceiptFailure(stage="post", reason="never-return")


@pytest.mark.django_db
@pytest.mark.parametrize("fails", [True, False])
def test_receipt_counts_usage_once_and_audits_only_failure(setup, fb_post, monkeypatch, fails):
    from apps.api_keys.models import ApiKeyAuditLog

    usage = Mock()
    monkeypatch.setattr("apps.api.middleware.record_usage", usage)
    if fails:
        fake_provider(monkeypatch, {"id": "456"}, APIError("never-return", status_code=403))
    else:
        fake_provider(monkeypatch, {"id": "456"}, page_post(object_id=None))
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == (502 if fails else 200)
    usage.assert_called_once()
    assert usage.call_args.kwargs["status_code"] == response.status_code
    assert usage.call_args.kwargs["action"] == "accounts.published_media"
    rows = ApiKeyAuditLog.objects.filter(action="accounts.published_media")
    assert rows.count() == (1 if fails else 0)
    if fails:
        assert rows.get().status_code == 502


@pytest.mark.django_db
@pytest.mark.parametrize("owner,video_id,expected", [("456", "987", 200), ("999", "987", 502), ("456", "654", 502)])
def test_deprecated_post_probe_requires_exact_owner_verified_video(
    setup, fb_post, monkeypatch, owner, video_id, expected
):
    error = APIError("never-return", status_code=400, raw_response={"error": {"code": 12, "message": "never-return"}})
    provider = fake_provider(monkeypatch, {"id": "456"}, error, video_node(id=video_id, **{"from": {"id": owner}}))
    before = (fb_post.status, fb_post.platform_post_id, fb_post.published_at)
    response = get(setup, setup.fb, f"published-media/{fb_post.id}")
    assert response.status_code == expected
    assert provider._request.call_count == 3
    assert provider._request.call_args_list[2].args[1].endswith("/987")
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)
    assert "never-return" not in response.content.decode()
    if expected == 200:
        body = response.json()
        assert body["receipt_node_type"] == "video" and body["video_id"] == "987"
        assert body["owner_id"] == "456" and body["public_visibility_verified"] is False
        assert body["post_is_published"] is None
    fb_post.refresh_from_db()
    assert (fb_post.status, fb_post.platform_post_id, fb_post.published_at) == before


@pytest.mark.parametrize("status,code", [(401, 12), (403, 12), (429, 12), (500, 12), (400, "12"), (400, 12.0)])
def test_deprecated_post_probe_only_accepts_typed_code12_http400(status, code):
    error = APIError("never-return", status_code=status, raw_response={"error": {"code": code}})
    assert adapter._post_type_probe_allowed(error) is False
