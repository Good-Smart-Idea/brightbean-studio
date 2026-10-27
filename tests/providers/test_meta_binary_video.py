import json
from unittest.mock import MagicMock

import pytest

from providers.exceptions import PublishError
from providers.facebook import FacebookProvider
from providers.instagram import InstagramProvider
from providers.meta_video import instagram_native_configuration, validate_meta_upload_uri
from providers.types import PostType, PublishContent


class MemoryProgress:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, timeout):
        self.data[key] = value


def response(data, status=200):
    return MagicMock(status_code=status, json=MagicMock(return_value=data))


@pytest.fixture
def content(tmp_path):
    path = tmp_path / "reaction.mp4"
    path.write_bytes(b"ftyp" * 1024)
    return PublishContent(
        text="Original caption",
        media_urls=["https://protected.invalid/private.mp4"],
        media_files=[str(path)],
        media_types=["video"],
        post_type=PostType.REEL,
        idempotency_key="child-1",
        upload_cache=MemoryProgress(),
    )


def instagram():
    return InstagramProvider({"ig_user_id": "1784", "facebook_user_access_token": "synthetic-user-grant"})


def test_instagram_raw_upload_uses_user_grant_and_no_public_url(content):
    provider = instagram()
    seen = []

    def request(method, url, **kwargs):
        seen.append((method, url, kwargs))
        if url.endswith("/media"):
            assert kwargs["json"] == {"caption": "Original caption", "media_type": "REELS", "upload_type": "resumable"}
            return response({"id": "container1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/container1"})
        if "rupload" in url:
            assert kwargs["headers"]["Authorization"] == "OAuth synthetic-user-grant"
            assert kwargs["headers"]["offset"] == "0"
            assert int(kwargs["headers"]["file_size"]) == 4096
            assert "file_url" not in kwargs["headers"]
            assert kwargs["data"].read(4) == b"ftyp"
            return response({"success": True})
        if url.endswith("/media_publish"):
            return response({"id": "published1"})
        return response({"status_code": "FINISHED"})

    provider._request = request
    assert provider.publish_post("synthetic-page-grant", content).platform_post_id == "published1"
    assert all(row[2].get("access_token", "synthetic-user-grant") == "synthetic-user-grant" for row in seen)
    assert "protected.invalid" not in repr(seen)


def test_processing_retry_reuses_upload_and_complete_replay(content):
    provider = instagram()
    provider._request = MagicMock(
        side_effect=[
            response({"id": "container1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/container1"}),
            response({"success": True}),
            response({"id": "published1"}),
        ]
    )
    provider._wait_for_container = MagicMock(side_effect=PublishError("processing timed out"))
    with pytest.raises(PublishError):
        provider.publish_post("page", content)
    assert provider._request.call_count == 2
    replacement = instagram()
    replacement._request = provider._request
    replacement._wait_for_container = MagicMock()
    assert replacement.publish_post("page", content).platform_post_id == "published1"
    replacement._wait_for_container.assert_called_once_with("synthetic-user-grant", "container1", should_stop=None)
    assert replacement._request.call_count == 3
    assert replacement.publish_post("page", content).platform_post_id == "published1"
    assert replacement._request.call_count == 3


@pytest.mark.parametrize("phase", ["creating", "uploading", "publishing"])
def test_ambiguous_phase_never_repeats_provider_write(content, phase):
    provider = instagram()
    provider._request = MagicMock(side_effect=RuntimeError("transport outcome unknown"))
    with pytest.raises(PublishError) as error:
        provider.publish_post("page", content)
    assert error.value.retryable is False
    key = next(iter(content.upload_cache.data))
    content.upload_cache.data[key] = json.dumps({"phase": phase, "upload_id": "container1"})
    provider._request.reset_mock()
    with pytest.raises(PublishError) as error:
        provider.publish_post("page", content)
    assert error.value.retryable is False
    provider._request.assert_not_called()


@pytest.mark.parametrize(
    "uri",
    [
        "http://rupload.facebook.com/ig-api-upload/v25.0/c1",
        "https://attacker.invalid/ig-api-upload/v25.0/c1",
        "https://rupload.facebook.com.evil.invalid/ig-api-upload/v25.0/c1",
        "https://user:pass@rupload.facebook.com/ig-api-upload/v25.0/c1",
        "https://rupload.facebook.com:443/ig-api-upload/v25.0/c1",
        "https://rupload.facebook.com/ig-api-upload/v25.0/other",
        "https://rupload.facebook.com/ig-api-upload/v25.0/c1?token=do-not-echo",
        "https://rupload.facebook.com/ig-api-upload/v25.0/c1#secret",
    ],
)
def test_unsafe_destination_rejected_without_echo(uri):
    with pytest.raises(PublishError) as error:
        validate_meta_upload_uri(uri, "c1", kind="ig-api-upload", platform="Instagram")
    assert uri not in str(error.value)


@pytest.mark.parametrize("failure", [RuntimeError("transport"), response({"success": False}), response({}, 302)])
def test_upload_failure_never_publishes(content, failure):
    provider = instagram()
    provider._request = MagicMock(
        side_effect=[
            response({"id": "container1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/container1"}),
            failure,
        ]
    )
    with pytest.raises(PublishError) as error:
        provider.publish_post("page", content)
    assert error.value.retryable is False
    assert provider._request.call_count == 2


@pytest.mark.parametrize(
    "credentials,blocker",
    [
        ({"ig_user_id": "1784"}, "missing_facebook_user_grant"),
        (
            {"ig_user_id": "1784", "facebook_user_access_token": "grant", "facebook_user_token_expires_at": 1},
            "expired_facebook_user_grant",
        ),
        (
            {
                "ig_user_id": "1784",
                "facebook_user_access_token": "grant",
                "facebook_user_token_expires_at": float("nan"),
            },
            "invalid_facebook_user_grant_expiry",
        ),
    ],
)
def test_missing_expired_invalid_user_grant_never_contacts_meta(content, credentials, blocker):
    provider = InstagramProvider(credentials)
    provider._request = MagicMock()
    assert instagram_native_configuration(credentials)["blocker"] == blocker
    with pytest.raises(PublishError):
        provider.publish_post("page-grant-is-not-fallback", content)
    provider._request.assert_not_called()


def test_target_override_never_contacts_meta(content):
    content.extra["ig_user_id"] = "different-account"
    provider = instagram()
    provider._request = MagicMock()
    with pytest.raises(PublishError):
        provider.publish_post("page", content)
    provider._request.assert_not_called()


def test_facebook_binary_reel_uses_page_grant(content):
    content.extra["page_id"] = "page1"
    provider = FacebookProvider({"page_id": "page1"})
    provider._request = MagicMock(
        side_effect=[
            response({"video_id": "video1", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/video1"}),
            response({"success": True}),
            response({"success": True}),
        ]
    )
    result = provider.publish_post("synthetic-page-grant", content)
    assert result.platform_post_id.endswith("video1")
    calls = provider._request.call_args_list
    assert calls[1].kwargs["headers"]["Authorization"] == "OAuth synthetic-page-grant"
    assert calls[2].kwargs["data"]["video_state"] == "PUBLISHED"
    assert "file_url" not in repr(calls)


def test_resume_survives_new_path_and_rotated_hosted_query(content, tmp_path):
    provider = instagram()
    provider._request = MagicMock(
        side_effect=[
            response({"id": "container1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/container1"}),
            response({"success": True}),
            response({"id": "published1"}),
        ]
    )
    provider._wait_for_container = MagicMock(side_effect=PublishError("processing timed out"))
    with pytest.raises(PublishError):
        provider.publish_post("page", content)
    new_path = tmp_path / "worker-recycled.mp4"
    with open(content.media_files[0], "rb") as original:
        new_path.write_bytes(original.read())
    content.media_files = [str(new_path)]
    content.media_urls = ["https://storage.invalid/video.mp4?rotated=another-query"]
    replacement = instagram()
    replacement._request = provider._request
    replacement._wait_for_container = MagicMock()
    assert replacement.publish_post("page", content).platform_post_id == "published1"
    assert len(content.upload_cache.data) == 1
    assert replacement._request.call_count == 3
