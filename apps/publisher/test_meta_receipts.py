"""Crash-window and immutable-byte regressions for the actual publisher seam."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from apps.composer.models import PlatformPost, Post
from apps.publisher import meta_receipts
from apps.publisher.engine import PublishEngine
from apps.publisher.models import PublishLog
from providers.exceptions import PublishError
from providers.types import PublishResult, PublishState, PublishStatus

from .test_meta_confirmation import native_row, sweep
from .test_meta_confirmation import receipt_case as receipt_case


def reset(case):
    case.child.publish_logs.all().delete()
    PlatformPost.objects.filter(pk=case.child.pk).update(
        platform_post_id="", status="publishing", native_upload_state={}
    )
    case.child.refresh_from_db()


def start(case, *, hosted=False):
    manifest = [] if hosted else meta_receipts.prepared_manifest([case.attachment], [case.asset.file.path])
    digest = meta_receipts.binding(
        case.child, grant_kind="account", content_type="video" if hosted else "reel", manifest=manifest
    )
    return meta_receipts.begin(
        case.child,
        expected_binding=digest,
        grant_kind="account",
        content_type="video" if hosted else "reel",
        manifest=manifest,
        native_resume=not hosted,
    )


def dispatch_patch(case):
    return patch("apps.publisher.engine.get_provider", return_value=case.provider)


@pytest.mark.django_db
def test_started_is_durable_before_provider_and_crash_cannot_resend(receipt_case):
    case = receipt_case
    reset(case)
    case.child.platform_extra = {}  # Regular hosted video has no native upload checkpoint.
    case.child.save(update_fields=["platform_extra"])

    def crash(*args):
        assert case.child.publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').exists()
        assert case.child.publish_logs.get().status_code is None
        raise KeyboardInterrupt("simulated process death after external submission")

    case.provider.publish_post = MagicMock(side_effect=crash)
    cache = MagicMock()
    with patch("apps.publisher.engine._provider_and_access_token", return_value=(case.provider, "synthetic")):
        with pytest.raises(KeyboardInterrupt):
            PublishEngine()._dispatch_to_provider(case.child, media_cache=cache)
        with pytest.raises(PublishError, match="unconfirmed") as error:
            PublishEngine()._dispatch_to_provider(case.child, media_cache=cache)
    assert error.value.retryable is False
    assert case.provider.publish_post.call_count == 1
    assert case.child.publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').count() == 1


@pytest.mark.django_db
def test_more_than_thirty_unrelated_logs_cannot_erase_started_guard(receipt_case):
    case = receipt_case
    reset(case)
    context = start(case, hosted=True)
    PublishLog.objects.bulk_create(
        [PublishLog(platform_post=case.child, response_body="unrelated", error_message="redacted") for _ in range(40)]
    )
    with pytest.raises(PublishError, match="unconfirmed"):
        start(case, hosted=True)
    assert meta_receipts._entries(case.child)[0]["attempt_id"] == context["marker"]["attempt_id"]


@pytest.mark.django_db
def test_existing_receipt_without_provenance_never_starts_new_attempt(receipt_case):
    case = receipt_case
    case.child.publish_logs.all().delete()
    with pytest.raises(PublishError, match="Existing Meta receipt"):
        start(case, hosted=True)
    assert not case.child.publish_logs.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["caption", "grant", "owner", "hold", "bytes"])
def test_post_return_drift_is_audited_without_attaching_receipt(receipt_case, change):
    case = receipt_case
    reset(case)
    context = start(case)
    if change == "caption":
        Post.objects.filter(pk=case.post.pk).update(caption="Changed")
    elif change == "grant":
        case.account.oauth_access_token = "rotated-grant"
        case.account.save()
    elif change == "owner":
        case.account.account_platform_id = "999"
        case.account.save(update_fields=["account_platform_id"])
    elif change == "hold":
        PlatformPost.objects.filter(pk=case.child.pk).update(status="on_hold")
    else:
        with open(case.asset.file.path, "wb") as stream:
            stream.write(b"different media bytes")
    meta_receipts.accept(
        case.child, {"meta_attempt": context, "platform_post_id": "555", "public_post_id": "777"}, duration_ms=1
    )
    case.child.refresh_from_db()
    assert case.child.platform_post_id == ""
    assert case.child.published_at is None
    assert case.child.publish_logs.filter(response_body__startswith='{"kind":"meta_publish_accepted"').exists()


@pytest.mark.django_db
@pytest.mark.parametrize("outcome", ["accepted", "retryable", "permanent"])
def test_stale_engine_saves_preserve_provider_checkpoints(receipt_case, outcome):
    case = receipt_case
    reset(case)
    context = start(case)
    checkpoint = {"key": "opaque", "progress": {"phase": "uploaded", "upload_id": "555"}}

    def provider_result(*args, **kwargs):
        PlatformPost.objects.filter(pk=case.child.pk).update(native_upload_state=checkpoint)
        if outcome != "accepted":
            raise PublishError("processing timed out", retryable=outcome == "retryable")
        return {
            "success": True,
            "async_publish": True,
            "platform_post_id": "555",
            "public_post_id": "555",
            "meta_attempt": context,
        }

    engine = PublishEngine()
    with patch.object(engine, "_dispatch_to_provider", side_effect=provider_result):
        engine._publish_platform_post(case.child)
    case.child.refresh_from_db()
    assert case.child.native_upload_state == checkpoint
    assert case.child.status == {"accepted": "publishing", "retryable": "scheduled", "permanent": "failed"}[outcome]


@pytest.mark.django_db
def test_changed_bytes_before_or_during_get_never_promote(receipt_case):
    case = receipt_case

    def check(*args):
        with open(case.asset.file.path, "wb") as stream:
            stream.write(b"changed after native GET")
        return PublishStatus(
            PublishState.COMPLETE,
            platform_post_id="555",
            raw={"id": "555", "owner_id": "111", "permalink": "https://www.facebook.com/reel/555/"},
        )

    case.provider.check_publish_status = MagicMock(side_effect=check)
    assert sweep(case) == 0
    assert case.child.status == "publishing"
    case.provider.check_publish_status.reset_mock()
    assert sweep(case) == 0
    case.provider.check_publish_status.assert_not_called()


@pytest.mark.django_db
def test_changed_copy_before_get_never_reuses_old_receipt(receipt_case):
    case = receipt_case
    Post.objects.filter(pk=case.post.pk).update(caption="New copy")
    case.provider._request = MagicMock(side_effect=AssertionError("no native GET on a changed attempt"))
    assert sweep(case) == 0
    case.provider._request.assert_not_called()
    assert case.child.status == "publishing"


@pytest.mark.django_db
def test_replay_preserves_typed_feed_mapping_without_resending(receipt_case):
    case = receipt_case
    reset(case)
    context = start(case)
    meta_receipts.accept(
        case.child, {"meta_attempt": context, "platform_post_id": "555", "public_post_id": "777"}, duration_ms=1
    )
    case.provider.publish_post = MagicMock(side_effect=AssertionError("accepted receipt must not be sent again"))
    cache = MagicMock()
    cache.path_for.return_value = case.asset.file.path
    with (
        dispatch_patch(case),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value=case.provider.credentials),
    ):
        result = PublishEngine()._dispatch_to_provider(case.child, media_cache=cache)
    assert result["platform_post_id"] == "555" and result["public_post_id"] == "777"
    assert result["async_publish"] is True
    case.provider.publish_post.assert_not_called()
    case.provider._request = MagicMock(return_value=httpx.Response(200, json=native_row()))
    assert sweep(case) == 1
    assert case.child.platform_post_id == "777"
    proof = json.loads(case.child.publish_logs.first().response_body)
    assert proof["native_id"] == "777" and proof["receipt_id"] == "555"
    assert case.child.native_post_url == "https://www.facebook.com/watch/?v=555"


@pytest.mark.django_db
def test_hosted_transport_has_no_byte_claim_or_publication_promotion(receipt_case):
    case = receipt_case
    reset(case)
    context = start(case, hosted=True)
    assert context["marker"]["manifest"] == []
    assert context["marker"]["media_bytes_verified"] is False
    meta_receipts.accept(
        case.child, {"meta_attempt": context, "platform_post_id": "555", "public_post_id": "555"}, duration_ms=1
    )
    case.provider._request = MagicMock(side_effect=AssertionError("unsupported byte identity"))
    assert sweep(case) == 0
    case.provider._request.assert_not_called()
    assert case.child.status == "publishing"


@pytest.mark.django_db
def test_real_native_resume_preserves_checkpoint_and_confirms_after_restart(receipt_case):
    from providers.instagram import InstagramProvider

    case = receipt_case
    reset(case)
    case.account.platform = "instagram"
    case.account.oauth_facebook_user_access_token = "synthetic-user-grant"
    case.account.save()
    case.child.social_account = case.account
    provider = InstagramProvider({"ig_user_id": "111", "facebook_user_access_token": "synthetic-user-grant"})
    provider._request = MagicMock(
        side_effect=[
            httpx.Response(200, json={"id": "222", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/222"}),
            httpx.Response(200, json={"success": True}),
            httpx.Response(200, json={"id": "555"}),
            httpx.Response(
                200,
                json={
                    "id": "555",
                    "owner": {"id": "111"},
                    "media_type": "VIDEO",
                    "permalink": "https://www.instagram.com/reel/realCode/",
                },
            ),
        ]
    )
    provider._wait_for_container = MagicMock(
        side_effect=[PublishError("Instagram container processing timed out"), None]
    )
    cache = MagicMock()
    cache.path_for.return_value = case.asset.file.path
    with (
        patch("apps.publisher.engine.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value=provider.credentials),
    ):
        assert PublishEngine()._publish_platform_post(case.child, media_cache=cache)["success"] is False
        case.child.refresh_from_db()
        assert case.child.native_upload_state["progress"]["phase"] == "uploaded"
        assert case.child.status == "scheduled"
        assert provider._request.call_count == 2
        PlatformPost.objects.filter(pk=case.child.pk).update(status="publishing")
        case.child.refresh_from_db()
        assert PublishEngine()._publish_platform_post(case.child, media_cache=cache)["success"] is True
        case.child.refresh_from_db()
        assert case.child.status == "publishing"
        assert case.child.native_upload_state["progress"]["phase"] == "complete"
        assert provider._request.call_count == 3
        assert PublishEngine().confirm_pending_publishes() == 1
    case.child.refresh_from_db()
    assert case.child.status == "published"
    assert case.child.native_post_url == "https://www.instagram.com/reel/realCode/"
    assert provider._request.call_count == 4
    assert [call.args[0] for call in provider._request.call_args_list] == ["POST", "POST", "POST", "GET"]
    started = case.child.publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"')
    assert started.count() == 1
    manifest = json.loads(started.get().response_body)["manifest"][0]
    assert manifest["size"] > 0 and len(manifest["sha256"]) == 64


@pytest.mark.django_db
def test_empty_receipt_is_nonretryable_not_published(receipt_case):
    case = receipt_case
    reset(case)
    case.post.media_attachments.all().delete()
    case.child.platform_extra = {}
    case.child.save(update_fields=["platform_extra"])
    case.provider.publish_post = MagicMock(return_value=PublishResult(platform_post_id=""))
    with patch("apps.publisher.engine._provider_and_access_token", return_value=(case.provider, "synthetic")):
        result = PublishEngine()._publish_platform_post(case.child)
    case.child.refresh_from_db()
    assert result["success"] is False
    assert case.child.status == "failed" and case.child.published_at is None
    assert case.child.next_retry_at is None
    case.provider.publish_post.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize("platform", ["facebook", "instagram"])
@pytest.mark.parametrize(
    "requested,allowed",
    [
        ("absent", True),
        ("111", True),
        (111, True),
        (None, False),
        (True, False),
        ("١١١", False),
        (" 111", False),
        ("999", False),
    ],
)
def test_prewrite_target_is_only_the_canonical_account(receipt_case, platform, requested, allowed):
    from providers.instagram import InstagramProvider

    case = receipt_case
    reset(case)
    case.account.platform = platform
    case.account.oauth_facebook_user_access_token = "synthetic-user-grant"
    case.account.save()
    case.child.social_account = case.account
    key = "page_id" if platform == "facebook" else "ig_user_id"
    extras = {"post_type": "reel"}
    if requested != "absent":
        extras[key] = requested
    case.child.platform_extra = extras
    case.child.save(update_fields=["platform_extra"])
    provider = (
        case.provider
        if platform == "facebook"
        else InstagramProvider({"ig_user_id": "111", "facebook_user_access_token": "synthetic-user-grant"})
    )
    provider.publish_post = MagicMock(return_value=PublishResult(platform_post_id="555", needs_confirmation=True))
    cache = MagicMock()
    cache.path_for.return_value = case.asset.file.path
    with (
        patch("apps.publisher.engine.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value=provider.credentials),
    ):
        if allowed:
            result = PublishEngine()._dispatch_to_provider(case.child, media_cache=cache)
            assert result["platform_post_id"] == "555"
            assert provider.publish_post.call_args.args[1].extra[key] == "111"
        else:
            with pytest.raises(PublishError, match="target does not match"):
                PublishEngine()._dispatch_to_provider(case.child, media_cache=cache)
            provider.publish_post.assert_not_called()
            assert not case.child.publish_logs.exists()
