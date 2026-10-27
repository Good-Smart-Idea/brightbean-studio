import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.composer.models import PlatformPost
from apps.publisher.native_uploads import NativeUploadCache
from providers.exceptions import PublishError

from .test_submit_for_review import review_case as review_case


@pytest.fixture
def native_case(review_case):
    account, key, child = review_case[2], review_case[3], review_case[6]
    account.platform = "instagram"
    account.account_platform_id = "1784"
    account.oauth_facebook_user_access_token = "synthetic-bound-user-grant"
    account.oauth_access_token = "synthetic-page-grant"
    account.save()
    key.permissions = ["create_posts", "publish_directly"]
    key.save(update_fields=["permissions"])
    child.status = "publishing"
    child.save(update_fields=["status"])
    return review_case


def readiness(case):
    return case[4].get(f"/api/v1/accounts/{case[2].id}/native-video-readiness", secure=True)


@pytest.mark.django_db
def test_configuration_is_token_free_and_not_live_eligibility(native_case):
    with patch("providers.base.SocialProvider._request", side_effect=AssertionError("no provider request")):
        response = readiness(native_case)
    assert response.status_code == 200, response.content
    assert response.json() == {
        "supported": True,
        "configured": True,
        "blocker": "",
        "provider_eligibility_verified": False,
    }
    assert "synthetic" not in response.content.decode()
    assert "https:" not in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("permission", ["create_posts", "publish_directly"])
def test_configuration_requires_both_permissions(native_case, permission):
    native_case[3].permissions = [permission]
    native_case[3].save(update_fields=["permissions"])
    assert readiness(native_case).status_code == 403


@pytest.mark.django_db
def test_configuration_account_allowlist(native_case):
    native_case[3].social_accounts.clear()
    assert readiness(native_case).status_code == 403


@pytest.mark.django_db
def test_configuration_reports_known_expired_user_grant(native_case):
    native_case[2].facebook_user_token_expires_at = timezone.now() - timedelta(seconds=1)
    native_case[2].save(update_fields=["facebook_user_token_expires_at"])
    assert readiness(native_case).json()["blocker"] == "expired_facebook_user_grant"


def write(cache, phase, **values):
    cache.set("a" * 64, json.dumps({"phase": phase, **values}), 3600)


@pytest.mark.django_db
def test_restart_adapter_reads_same_uploaded_session(native_case):
    child = native_case[6]
    cache = NativeUploadCache(child)
    assert cache.get("a" * 64) is None
    write(cache, "creating")
    write(cache, "created", upload_id="container1")
    write(cache, "uploading", upload_id="container1")
    write(cache, "uploaded", upload_id="container1")
    restarted = NativeUploadCache(PlatformPost.objects.get(pk=child.pk))
    assert json.loads(restarted.get("a" * 64)) == {"phase": "uploaded", "upload_id": "container1"}
    write(restarted, "publishing", upload_id="container1")
    write(restarted, "complete", upload_id="container1", media_id="published1")
    child.refresh_from_db()
    assert child.native_upload_state["progress"]["media_id"] == "published1"
    assert child.platform_extra == {}
    assert child.platform_post_id == ""


@pytest.mark.django_db
def test_competing_adapter_cannot_overwrite_phase(native_case):
    first, second = NativeUploadCache(native_case[6]), NativeUploadCache(native_case[6])
    first.get("a" * 64)
    second.get("a" * 64)
    write(first, "creating")
    with pytest.raises(PublishError, match="ownership changed"):
        write(second, "creating")


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["hold", "caption", "account", "grant"])
def test_revocation_or_content_race_blocks_checkpoint(native_case, change):
    child = native_case[6]
    cache = NativeUploadCache(child)
    cache.get("a" * 64)
    if change == "hold":
        PlatformPost.objects.filter(pk=child.pk).update(status="on_hold")
    elif change == "caption":
        native_case[5].caption = "Changed"
        native_case[5].save(update_fields=["caption"])
    elif change == "account":
        native_case[2].account_platform_id = "different"
        native_case[2].save(update_fields=["account_platform_id"])
    else:
        native_case[2].oauth_facebook_user_access_token = "different-synthetic-grant"
        native_case[2].save()
    with pytest.raises(PublishError, match="authorization or content changed"):
        write(cache, "creating")
    child.refresh_from_db()
    assert child.native_upload_state == {}


@pytest.mark.django_db
def test_progress_cannot_skip_phase_or_change_fingerprint(native_case):
    cache = NativeUploadCache(native_case[6])
    cache.get("a" * 64)
    with pytest.raises(PublishError, match="phase transition"):
        write(cache, "publishing", upload_id="container1")
    write(cache, "creating")
    with pytest.raises(PublishError, match="content changed"):
        NativeUploadCache(native_case[6]).get("b" * 64)


def test_progress_field_is_internal():
    assert PlatformPost._meta.get_field("native_upload_state").editable is False


@pytest.mark.django_db
def test_clone_does_not_inherit_native_session(native_case):
    from apps.composer.services import clone_post

    child = native_case[6]
    child.native_upload_state = {"key": "a" * 64, "progress": {"phase": "publishing", "upload_id": "container1"}}
    child.save(update_fields=["native_upload_state"])
    clone = clone_post(native_case[5], author=native_case[0])
    assert clone.platform_posts.get().native_upload_state == {}


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["caption", "bytes"])
def test_changed_content_cannot_reset_ambiguous_session(native_case, tmp_path, change):
    from unittest.mock import MagicMock

    from providers.instagram import InstagramProvider
    from providers.types import PostType, PublishContent

    path = tmp_path / "video.mp4"
    path.write_bytes(b"original-video")
    child = native_case[6]
    provider = InstagramProvider({"ig_user_id": "1784", "facebook_user_access_token": "synthetic-bound-user-grant"})
    provider._request = MagicMock(side_effect=RuntimeError("ambiguous"))
    content = PublishContent(
        text=child.effective_caption,
        media_files=[str(path)],
        post_type=PostType.REEL,
        idempotency_key=str(child.pk),
        upload_cache=NativeUploadCache(child),
    )
    with pytest.raises(PublishError):
        provider.publish_post("page", content)
    if change == "caption":
        content.text = "Different caption"
    else:
        path.write_bytes(b"different-video")
    content.upload_cache = NativeUploadCache(PlatformPost.objects.get(pk=child.pk))
    provider._request.reset_mock()
    with pytest.raises(PublishError, match="content changed"):
        provider.publish_post("page", content)
    provider._request.assert_not_called()


@pytest.mark.django_db
def test_engine_uses_storage_local_path_without_refresh_or_http_download(native_case, tmp_path):
    from unittest.mock import MagicMock

    from apps.composer.models import PostMedia
    from apps.media_library.models import MediaAsset
    from apps.publisher.engine import PublishEngine
    from providers.instagram import InstagramProvider
    from providers.types import PublishResult

    asset = MediaAsset.objects.create(
        workspace=native_case[1],
        organization=native_case[1].organization,
        file="native/reaction.mp4",
        filename="reaction.mp4",
        media_type="video",
        duration=54.5,
        processing_status="completed",
    )
    PostMedia.objects.create(post=native_case[5], media_asset=asset)
    local = tmp_path / "cached.mp4"
    local.write_bytes(b"video")
    cache = MagicMock()
    cache.path_for.return_value = str(local)
    provider = InstagramProvider({"ig_user_id": "1784", "facebook_user_access_token": "synthetic-bound-user-grant"})
    provider.publish_post = MagicMock(return_value=PublishResult(platform_post_id="result1"))
    with (
        patch("apps.publisher.engine.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value=provider.credentials),
        patch("apps.publisher.engine._provider_and_access_token", side_effect=AssertionError("must not refresh")),
    ):
        PublishEngine()._dispatch_to_provider(native_case[6], media_cache=cache)
    cache.path_for.assert_called_once_with(asset)
    sent = provider.publish_post.call_args.args[1]
    assert sent.media_files == [str(local)]
    assert isinstance(sent.upload_cache, NativeUploadCache)
