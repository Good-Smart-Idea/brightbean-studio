"""Independent Meta protocol fixtures: a delivery receipt is not publication proof."""

from unittest.mock import Mock

import httpx
import pytest

from providers.exceptions import APIError, PublishError
from providers.facebook import FacebookProvider
from providers.instagram import InstagramProvider
from providers.types import PublishState


def fb_video(**changes):
    return {
        "id": "555",
        "from": {"id": "111"},
        "status": {"video_status": "ready"},
        "published": True,
        "permalink_url": "https://www.facebook.com/reel/555/",
        **changes,
    }


def ig_media(**changes):
    return {
        "id": "555",
        "owner": {"id": "111"},
        "media_type": "VIDEO",
        "permalink": "https://www.instagram.com/reel/validShortcode/",
        **changes,
    }


def provider_for(platform, *bodies, target="111", extra=None):
    cls = FacebookProvider if platform == "facebook" else InstagramProvider
    key = "page_id" if platform == "facebook" else "ig_user_id"
    provider = cls({key: target, **(extra or {})})
    provider._request = Mock(
        side_effect=[x if isinstance(x, Exception) else httpx.Response(200, json=x) for x in bodies]
    )
    return provider


def read_only(provider, maximum):
    assert provider._request.call_count <= maximum
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"published": False}, PublishState.PENDING),
        ({"status": {"video_status": "processing"}}, PublishState.PENDING),
        ({"published": None}, PublishState.UNCONFIRMED),
        ({"published": "true"}, PublishState.UNCONFIRMED),
        ({"published": 1}, PublishState.UNCONFIRMED),
        ({"status": {"video_status": "unknown"}}, PublishState.UNCONFIRMED),
        ({"status": {}}, PublishState.UNCONFIRMED),
        ({"status": None}, PublishState.UNCONFIRMED),
        ({"from": {}}, PublishState.UNCONFIRMED),
        ({"from": None}, PublishState.UNCONFIRMED),
        ({"permalink_url": ""}, PublishState.UNCONFIRMED),
        ({"id": "556"}, PublishState.UNCONFIRMED),
    ],
)
def test_facebook_requires_all_positive_native_evidence(changes, expected):
    provider = provider_for("facebook", fb_video(**changes))
    status = provider.check_publish_status("synthetic-token", "555")
    assert status.state is expected
    read_only(provider, 1)


def test_facebook_ready_published_exact_owned_video_confirms():
    provider = provider_for("facebook", fb_video())
    status = provider.check_publish_status("synthetic-token", "555")
    assert status.state is PublishState.COMPLETE
    assert status.platform_post_id == "555"
    read_only(provider, 1)


@pytest.mark.parametrize("phase", ["uploading_phase", "processing_phase", "publishing_phase"])
@pytest.mark.parametrize("failure", ["error", "failed"])
def test_facebook_explicit_phase_failure_is_terminal(phase, failure):
    provider = provider_for("facebook", fb_video(status={"video_status": "ready", phase: {"status": failure}}))
    assert provider.check_publish_status("synthetic-token", "555").state is PublishState.FAILED
    read_only(provider, 1)


@pytest.mark.parametrize(
    "platform,row", [("facebook", fb_video(**{"from": {"id": "999"}})), ("instagram", ig_media(owner={"id": "999"}))]
)
def test_native_foreign_owner_is_not_promoted_or_probed(platform, row):
    provider = provider_for(platform, row)
    assert provider.check_publish_status("synthetic-token", "555").state is PublishState.FAILED
    assert provider._request.call_count == 1
    read_only(provider, 1)


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
@pytest.mark.parametrize("handle", ["١٢٣", "-555", "555_666", "https://foreign.example/555", "5" * 4096])
def test_unsafe_or_ambiguous_receipt_never_reaches_graph(platform, handle):
    provider = provider_for(platform)
    assert provider.check_publish_status("synthetic-token", handle).state is PublishState.FAILED
    provider._request.assert_not_called()


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
def test_unicode_bound_account_never_reaches_graph(platform):
    provider = provider_for(platform, target="١١١")
    assert provider.check_publish_status("synthetic-token", "555").state is PublishState.FAILED
    provider._request.assert_not_called()


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://www.facebook.com/watch/?v=555&access_token=secret&tracking=discard",
            "https://www.facebook.com/watch/?v=555",
        ),
        (
            "https://www.facebook.com/photo.php?fbid=555&access_token=secret",
            "https://www.facebook.com/photo.php?fbid=555",
        ),
    ],
)
def test_facebook_keeps_essential_numeric_link_identity_only(url, expected):
    provider = provider_for("facebook", fb_video(permalink_url=url))
    status = provider.check_publish_status("synthetic-token", "555")
    assert status.state is PublishState.COMPLETE
    assert status.raw["permalink"] == expected
    read_only(provider, 1)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/watch/",
        "https://www.facebook.com/watch/?v=not-a-number",
        "https://www.facebook.com/watch/?v=١٢٣",
        "https://www.facebook.com/photo.php",
        "https://user:secret@www.facebook.com/reel/555/",
        "https://foreign.example/reel/555/",
    ],
)
def test_facebook_unusable_or_foreign_permalink_is_not_evidence(url):
    provider = provider_for("facebook", fb_video(permalink_url=url))
    assert provider.check_publish_status("synthetic-token", "555").state is PublishState.UNCONFIRMED
    read_only(provider, 1)


@pytest.mark.parametrize("media_type", ["VIDEO", "IMAGE", "CAROUSEL_ALBUM"])
def test_instagram_uses_igmedia_proof_without_facebook_video_fields(media_type):
    provider = provider_for(
        "instagram",
        ig_media(
            media_type=media_type,
            permalink="https://www.instagram.com/" + ("reel" if media_type == "VIDEO" else "p") + "/validShortcode/",
        ),
    )
    status = provider.check_publish_status("synthetic-token", "555")
    assert status.state is PublishState.COMPLETE
    assert status.platform_post_id == "555"
    fields = provider._request.call_args.kwargs["params"]["fields"]
    assert "status" not in fields and "published" not in fields
    read_only(provider, 1)


@pytest.mark.parametrize("changes", [{"media_type": "UNKNOWN"}, {"media_type": None}, {"permalink": ""}, {"id": "556"}])
def test_instagram_incomplete_metadata_remains_unknown(changes):
    provider = provider_for("instagram", ig_media(**changes))
    assert provider.check_publish_status("synthetic-token", "555").state is PublishState.UNCONFIRMED
    read_only(provider, 1)


@pytest.mark.parametrize("present", [True, False])
def test_instagram_missing_owner_uses_one_bounded_scoped_membership_page(present):
    page = {"data": [{"id": "555"}] if present else [], "paging": {"next": "https://foreign.example/never-follow"}}
    provider = provider_for("instagram", ig_media(owner=None), page)
    state = provider.check_publish_status("synthetic-token", "555").state
    assert state is (PublishState.COMPLETE if present else PublishState.UNCONFIRMED)
    request = provider._request.call_args
    assert request.args[1].endswith("/111/media")
    assert request.kwargs["params"]["limit"] <= 50
    assert provider._request.call_count == 2
    read_only(provider, 2)


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
@pytest.mark.parametrize("error", [APIError("redacted", status_code=403), TimeoutError("redacted")])
def test_native_read_failure_cannot_trigger_upload_or_alternate_probe(platform, error):
    provider = provider_for(platform, error)
    with pytest.raises(type(error)):
        provider.check_publish_status("synthetic-token", "555")
    assert provider._request.call_count == 1
    read_only(provider, 1)


def test_native_instagram_confirmation_uses_exact_saved_user_grant():
    provider = provider_for(
        "instagram",
        ig_media(),
        extra={"require_native_user_confirmation": True, "facebook_user_access_token": "synthetic-user"},
    )
    assert provider.check_publish_status("synthetic-page", "555").state is PublishState.COMPLETE
    assert provider._request.call_args.kwargs["access_token"] == "synthetic-user"
    read_only(provider, 1)


def test_native_instagram_confirmation_never_falls_back_to_page_grant():
    provider = provider_for("instagram", extra={"require_native_user_confirmation": True})
    with pytest.raises(PublishError):
        provider.check_publish_status("synthetic-page", "555")
    provider._request.assert_not_called()


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
@pytest.mark.parametrize("owner", [True, "unknown", "٩٩٩", "9" * 4096])
def test_malformed_owner_is_unknown_not_a_definitive_foreign_account(platform, owner):
    row = fb_video(**{"from": {"id": owner}}) if platform == "facebook" else ig_media(owner={"id": owner})
    provider = provider_for(platform, row)
    assert provider.check_publish_status("synthetic-token", "555").state is PublishState.UNCONFIRMED
    read_only(provider, 1)
