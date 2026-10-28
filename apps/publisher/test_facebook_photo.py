"""Single-photo contracts using tiny images, fake storage/HTTP, and test DB only."""

import copy
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.utils import timezone

from apps.api.tests.test_submit_for_review import review_case as review_case
from apps.composer.models import PlatformPost, PostMedia
from apps.media_library.models import MediaAsset
from apps.publisher import image_delivery, meta_receipts
from apps.publisher.engine import PublishEngine
from apps.publisher.models import PublishLog
from apps.publisher.test_image_delivery import FakeLocalStorage, encoded
from apps.social_accounts.models import SocialAccount
from providers.exceptions import APIError, PublishError
from providers.facebook import FacebookProvider
from providers.meta_confirmation import facebook_photo_status, facebook_story_id, native_id
from providers.types import PostType, PreparedPhotoUpload, PublishContent, PublishState


@pytest.fixture
def photo_case(review_case, monkeypatch):
    account, post, child = review_case[2], review_case[5], review_case[6]
    account.platform = "facebook"
    account.account_platform_id = "123"
    account.oauth_access_token = "synthetic-photo-grant"
    account.token_expires_at = timezone.now() + timedelta(days=1)
    account.save()
    child.status = "publishing"
    child.save(update_fields=["status"])
    storage = FakeLocalStorage(encoded())
    monkeypatch.setattr(MediaAsset._meta.get_field("file"), "storage", storage)
    monkeypatch.setattr(image_delivery, "default_storage", storage)
    monkeypatch.setattr(image_delivery.storage, "is_s3_backend", lambda: False)
    asset = MediaAsset.objects.create(
        organization=review_case[1].organization,
        workspace=review_case[1],
        file="synthetic/private.png",
        filename="original.png",
        media_type="image",
        mime_type="image/png",
        processing_status="completed",
    )
    PostMedia.objects.create(post=post, media_asset=asset)
    return review_case, asset, storage


@contextmanager
def engine_http(case, handler):
    """Use the real provider/base HTTP encoder with a nonnetwork transport."""
    client = httpx.Client
    calls = []

    def transport(request):
        calls.append(request)
        return handler(request)

    with (
        patch(
            "providers.base.httpx.Client",
            side_effect=lambda **kw: client(transport=httpx.MockTransport(transport), **kw),
        ),
        patch(
            "apps.publisher.engine._resolve_publish_credentials",
            return_value={"page_id": "123", "native_video_account_connected": True},
        ),
        patch("apps.publisher.engine._provider_and_access_token", side_effect=AssertionError("no refresh")),
        patch("apps.publisher.engine.download_to_path", side_effect=AssertionError("no unbounded download")),
    ):
        yield PublishEngine(), calls


def photo_response(request):
    if request.method == "POST":
        assert request.url.path == "/v25.0/123/photos"
        return httpx.Response(200, json={"id": "456", "post_id": "123_789"})
    if request.url.path == "/v25.0/456":
        return httpx.Response(
            200, json={"id": "456", "from": {"id": "123"}, "name": "Please review", "page_story_id": "123_789"}
        )
    assert request.url.path == "/v25.0/123_789"
    return httpx.Response(
        200,
        json={
            "id": "123_789",
            "from": {"id": "123"},
            "message": "Please review",
            "is_published": True,
            "is_hidden": False,
            "permalink_url": "https://www.facebook.com/123/posts/789",
        },
    )


def prepared(data=None, authorize=lambda: None):
    data = encoded() if data is None else data
    return PreparedPhotoUpload(
        "synthetic-asset", data, hashlib.sha256(data).hexdigest(), "image/png", "123", "caption", authorize
    )


@pytest.mark.parametrize(
    "story,allowed",
    [
        ("123_789", True),
        ("789", True),
        ("999_789", False),
        ("123_789_1", False),
        (True, False),
        ("bad", False),
        ("１２３", False),
    ],
)
def test_photo_story_type_does_not_widen_numeric_native_id(story, allowed):
    assert facebook_story_id(story, "123") is allowed
    assert native_id("123_789") is False


@pytest.mark.django_db
def test_engine_one_exact_multipart_then_native_photo_and_story_confirmation(photo_case):
    case, asset, storage = photo_case
    child = case[6]
    child.platform_extra = {"thumbnail_asset_id": "unused", "cover_image_asset_id": "unused"}
    child.save(update_fields=["platform_extra"])
    before = copy.deepcopy(child.platform_extra)
    original = storage.data
    with engine_http(case, photo_response) as (engine, calls):
        result = engine._publish_platform_post(child)
        child.refresh_from_db()
        assert result["success"] is True, result
        assert child.status == "publishing" and child.platform_post_id == "456"
        assert child.publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').count() == 1
        assert child.publish_logs.filter(response_body__startswith='{"kind":"meta_publish_accepted"').count() == 1
        # Accepted replay keeps the receipt and never repeats the photo POST.
        again = engine._publish_platform_post(child)
        assert again["success"] is True and len(calls) == 1
        assert engine.confirm_pending_publishes() == 1
    child.refresh_from_db()
    assert child.status == "published" and child.platform_post_id == "123_789"
    assert [r.method for r in calls] == ["POST", "GET", "GET"]
    body = calls[0].content
    assert original in body and b'name="source"' in body and b"image/png" in body
    assert b'name="published"' in body and b"true" in body and b'name="caption"' in body
    assert b"Please review" in body and b'name="url"' not in body and b'name="message"' not in body
    assert b"attached_media" not in body and all("/feed" not in str(r.url) for r in calls)
    assert child.platform_extra == before and storage.data == original
    assert all(stream.closed for stream in storage.streams)
    accepted = meta_receipts._entries(child)  # Published row keeps immutable history.
    assert any(v.get("public_post_id") == "123_789" for v in accepted)


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["timeout", "malformed", "partial_photo", "foreign_story", "http_error"])
def test_uncertain_write_keeps_started_and_never_replays(photo_case, failure):
    case, _, _ = photo_case
    child = case[6]

    def failed(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE body or grant", request=request)
        if failure == "malformed":
            return httpx.Response(200, content=b"PRIVATE malformed")
        if failure == "partial_photo":
            return httpx.Response(200, json={"id": "456"})
        if failure == "foreign_story":
            return httpx.Response(200, json={"id": "456", "post_id": "999_789"})
        return httpx.Response(403, json={"error": {"message": "PRIVATE", "code": 190}})

    with engine_http(case, failed) as (engine, calls):
        result = engine._publish_platform_post(child)
        child.refresh_from_db()
        assert not result["success"] and child.status == "failed"
        assert child.retry_count == 0 and child.next_retry_at is None
        assert child.platform_post_id == "" and child.native_upload_state == {}
        assert "PRIVATE" not in json.dumps(list(child.publish_logs.values("error_message", "response_body")))
        assert len(calls) == 1
        child.status = "publishing"  # Synthetic interrupted-attempt re-entry, not an application action.
        child.save(update_fields=["status"])
        engine._publish_platform_post(child)
        assert len(calls) == 1
    assert child.publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').count() == 1
    assert not child.publish_logs.filter(response_body__startswith='{"kind":"meta_publish_accepted"').exists()


@pytest.mark.django_db
def test_receipt_save_failure_is_terminal_and_keeps_intent(photo_case):
    case, _, _ = photo_case
    with (
        engine_http(case, photo_response) as (engine, calls),
        patch.object(meta_receipts, "accept", side_effect=RuntimeError("PRIVATE")),
    ):
        result = engine._publish_platform_post(case[6])
    case[6].refresh_from_db()
    assert not result["success"] and case[6].status == "failed" and case[6].next_retry_at is None
    assert case[6].retry_count == 0 and len(calls) == 1
    assert case[6].publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').exists()
    assert "PRIVATE" not in result["error"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change",
    ["grant", "caption", "bytes", "hold", "expired", "reconnect", "scope", "permission", "owner", "post_scope"],
)
def test_fresh_authorization_changes_after_intent_stop_before_post(photo_case, change):
    case, asset, storage = photo_case
    original_begin = meta_receipts.begin

    def begin_then_change(*args, **kwargs):
        context = original_begin(*args, **kwargs)
        if change == "grant":
            case[2].oauth_access_token = "changed-synthetic"
            case[2].save()
        elif change == "caption":
            case[5].caption = "Changed after intent"
            case[5].save(update_fields=["caption"])
        elif change == "bytes":
            storage.data = encoded("JPEG", "RGB")
        elif change == "hold":
            sibling = SocialAccount.objects.create(
                workspace=case[1], platform="facebook", account_platform_id="999", account_name="Held sibling"
            )
            PlatformPost.objects.create(post=case[5], social_account=sibling, status="on_hold")
        elif change == "expired":
            case[2].token_expires_at = timezone.now() - timedelta(seconds=1)
            case[2].save(update_fields=["token_expires_at"])
        elif change == "reconnect":
            case[2].connection_status = "error"
            case[2].save(update_fields=["connection_status"])
        elif change == "owner":
            case[2].account_platform_id = "999"
            case[2].save(update_fields=["account_platform_id"])
        elif change == "post_scope":
            from apps.workspaces.models import Workspace

            case[5].workspace = Workspace.objects.create(name="Moved post", organization=case[1].organization)
            case[5].save(update_fields=["workspace"])
        elif change == "permission":
            case[2].missing_scopes = ["pages_manage_posts"]
            case[2].save(update_fields=["missing_scopes"])
        else:
            asset.organization_id = None
            asset.save(update_fields=["organization"])
        return context

    with (
        engine_http(case, photo_response) as (engine, calls),
        patch.object(meta_receipts, "begin", side_effect=begin_then_change),
    ):
        result = engine._publish_platform_post(case[6])
    assert not result["success"] and calls == []


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["missing", "not_completed", "webp", "foreign_org", "legacy_history"])
def test_invalid_or_legacy_image_stops_before_write(photo_case, change):
    case, asset, storage = photo_case
    if change == "missing":
        asset.file = ""
        asset.save(update_fields=["file"])
    elif change == "not_completed":
        asset.processing_status = "pending"
        asset.save(update_fields=["processing_status"])
    elif change == "webp":
        storage.data = encoded("WEBP")
        asset.mime_type = "image/webp"
        asset.save(update_fields=["mime_type"])
    elif change == "foreign_org":
        asset.organization_id = None
        asset.save(update_fields=["organization"])
    else:
        PublishLog.objects.create(platform_post=case[6], attempt_number=1, error_message="Legacy uncertain attempt")
    with engine_http(case, photo_response) as (engine, calls):
        assert not engine._publish_platform_post(case[6])["success"]
        assert calls == []
    assert not case[6].publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').exists()


@pytest.mark.django_db
def test_clone_has_no_receipts_progress_or_prepared_payload(photo_case):
    from apps.composer.services import clone_post

    case, _, _ = photo_case
    with engine_http(case, photo_response) as (engine, _):
        assert engine._publish_platform_post(case[6])["success"]
    clone = clone_post(case[5], author=case[0])
    child = clone.platform_posts.get()
    assert child.status == "draft" and child.platform_post_id == "" and child.native_upload_state == {}
    assert not child.publish_logs.exists() and child.platform_extra == {}


@pytest.mark.parametrize(
    "field,value", [("data", b"changed"), ("owner_id", "999"), ("caption", "changed"), ("mime_type", "image/webp")]
)
def test_prepared_byte_or_scope_mismatch_never_calls_provider(field, value):
    photo = replace(prepared(), **{field: value})
    provider = FacebookProvider({"page_id": "123"})
    provider._request = MagicMock()
    with pytest.raises(PublishError):
        provider.publish_post(
            "synthetic",
            PublishContent(text="caption", post_type=PostType.IMAGE, extra={"page_id": "123"}, prepared_photo=photo),
        )
    provider._request.assert_not_called()


@pytest.mark.parametrize(
    "which,field,value",
    [
        ("photo", "id", "999"),
        ("photo", "from", {"id": "999"}),
        ("photo", "name", "wrong"),
        ("photo", "page_story_id", None),
        ("photo", "page_story_id", "123_999"),
        ("post", "id", "123_999"),
        ("post", "from", {"id": "999"}),
        ("post", "message", "wrong"),
        ("post", "is_published", 1),
        ("post", "is_hidden", 0),
        ("post", "permalink_url", "https://evil.invalid/789"),
    ],
)
def test_native_confirmation_requires_exact_photo_story_owner_caption_and_visibility(which, field, value):
    photo = {"id": "456", "from": {"id": "123"}, "name": "caption", "page_story_id": "123_789"}
    post = {
        "id": "123_789",
        "from": {"id": "123"},
        "message": "caption",
        "is_published": True,
        "is_hidden": False,
        "permalink_url": "https://www.facebook.com/123/posts/789",
    }
    (photo if which == "photo" else post)[field] = value
    provider = FacebookProvider(
        {"page_id": "123", "native_photo_story_id": "123_789", "native_photo_caption": "caption"}
    )
    provider._request = MagicMock(side_effect=[httpx.Response(200, json=photo), httpx.Response(200, json=post)])
    status = facebook_photo_status(provider, "synthetic", "456")
    assert status.state == PublishState.UNCONFIRMED
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)
    assert len(provider._request.call_args_list) <= 2


@pytest.mark.django_db(transaction=True)
def test_intent_is_committed_and_transaction_closed_inside_photo_post(photo_case):
    from django.db import connection

    case, _, _ = photo_case

    def assert_committed(request):
        assert not connection.in_atomic_block
        assert case[6].publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').exists()
        return photo_response(request)

    with engine_http(case, assert_committed) as (engine, calls):
        assert engine._publish_platform_post(case[6])["success"]
    assert len(calls) == 1 and calls[0].method == "POST"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scope,allowed",
    [
        ("pages_manage_posts", False),
        ("pages_read_engagement", False),
        ("pages_show_list", False),
        ("pages_messaging", True),
    ],
)
def test_only_known_photo_permission_negatives_block_fresh_write(photo_case, scope, allowed):
    case, _, _ = photo_case
    begin = meta_receipts.begin

    def begin_then_scope(*args, **kwargs):
        context = begin(*args, **kwargs)
        case[2].missing_scopes = [scope]
        case[2].save(update_fields=["missing_scopes"])
        return context

    with (
        engine_http(case, photo_response) as (engine, calls),
        patch.object(meta_receipts, "begin", side_effect=begin_then_scope),
    ):
        assert engine._publish_platform_post(case[6])["success"] is allowed
    assert len(calls) == int(allowed)


@pytest.mark.parametrize("which", ["photo", "post"])
@pytest.mark.parametrize("failure", ["api", "timeout", "json"])
def test_confirmation_read_failures_are_closed_get_only_without_raw_messages(which, failure):
    photo = httpx.Response(
        200, json={"id": "456", "from": {"id": "123"}, "name": "caption", "page_story_id": "123_789"}
    )
    bad = (
        APIError("PRIVATE raw Graph body", status_code=403)
        if failure == "api"
        else httpx.ReadTimeout("PRIVATE signed token")
        if failure == "timeout"
        else httpx.Response(200, content=b"PRIVATE invalid JSON")
    )
    provider = FacebookProvider(
        {"page_id": "123", "native_photo_story_id": "123_789", "native_photo_caption": "caption"}
    )
    provider._request = MagicMock(side_effect=[bad] if which == "photo" else [photo, bad])
    status = facebook_photo_status(provider, "synthetic", "456")
    assert status.state == PublishState.UNCONFIRMED and "PRIVATE" not in repr(status)
    assert all(call.args[0] == "GET" for call in provider._request.call_args_list)


@pytest.mark.django_db
def test_all_image_verification_reads_stay_on_bounded_s3_range(photo_case, monkeypatch):
    import io

    from django.db.models.fields.files import FieldFile

    case, _, local = photo_case
    remote = object()
    monkeypatch.setattr(MediaAsset._meta.get_field("file"), "storage", remote)
    monkeypatch.setattr(image_delivery, "default_storage", remote)
    monkeypatch.setattr(image_delivery.storage, "is_s3_backend", lambda: True)
    opened = []

    def ranged(key, start, end):
        assert key == "synthetic/private.png" and start == 0 and end == image_delivery.MAX_INSPECTION_BYTES
        stream = io.BytesIO(local.data)
        opened.append(stream)
        return stream

    with (
        patch.object(image_delivery.storage, "open_object_range", side_effect=ranged),
        patch.object(FieldFile, "open", side_effect=AssertionError("no S3 eager spool")),
        engine_http(case, photo_response) as (engine, calls),
    ):
        assert engine._publish_platform_post(case[6])["success"]
        assert engine.confirm_pending_publishes() == 1
    assert len(calls) == 3 and len(opened) >= 5 and all(stream.closed for stream in opened)


@pytest.mark.django_db
def test_foreign_account_workspace_stops_before_any_private_storage_open(photo_case):
    from apps.workspaces.models import Workspace

    case, _, storage = photo_case
    case[2].workspace = Workspace.objects.create(name="Foreign target", organization=case[1].organization)
    case[2].save(update_fields=["workspace"])
    with engine_http(case, photo_response) as (engine, calls):
        result = engine._publish_platform_post(case[6])
    assert not result["success"] and calls == [] and storage.calls == []
    assert not case[6].publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').exists()


@pytest.mark.django_db(transaction=True)
def test_process_death_during_http_retains_durable_intent_and_no_replay(photo_case):
    case, _, _ = photo_case

    def interrupted(request):
        raise SystemExit("synthetic worker death after request entry")

    with engine_http(case, interrupted) as (engine, calls):
        with pytest.raises(SystemExit):
            engine._publish_platform_post(case[6])
        case[6].refresh_from_db()
        assert case[6].status == "publishing" and case[6].platform_post_id == ""
        assert case[6].publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').count() == 1
        assert not engine._publish_platform_post(case[6])["success"]
        assert len(calls) == 1
    case[6].refresh_from_db()
    assert case[6].status == "failed" and case[6].next_retry_at is None


@pytest.mark.django_db
def test_same_prepared_payload_cannot_authorize_a_second_write(photo_case):
    case, _, _ = photo_case
    captured = []
    original = FacebookProvider.publish_post

    def capture(provider, token, content):
        captured.append(content)
        return original(provider, token, content)

    with engine_http(case, photo_response) as (engine, calls), patch.object(FacebookProvider, "publish_post", capture):
        assert engine._publish_platform_post(case[6])["success"]
        assert type(captured[0].prepared_photo.data) is bytes
        with pytest.raises(PublishError, match="already authorized once"):
            original(FacebookProvider({"page_id": "123"}), "synthetic-photo-grant", captured[0])
        assert len(calls) == 1


def test_photo_story_id_length_is_bounded_before_splitting():
    assert not facebook_story_id("123_" + "9" * 1000, "123")


@pytest.mark.django_db
def test_photo_accepted_ledger_retains_actual_201_status(photo_case):
    case, _, _ = photo_case
    with engine_http(case, lambda _: httpx.Response(201, json={"id": "456", "post_id": "123_789"})) as (engine, calls):
        assert engine._publish_platform_post(case[6])["success"]
    accepted = case[6].publish_logs.get(response_body__startswith='{"kind":"meta_publish_accepted"')
    assert accepted.status_code == 201 and len(calls) == 1


@pytest.mark.django_db
def test_actual_receipt_state_save_failure_rolls_back_ack_and_never_resends(photo_case):
    case, _, _ = photo_case
    original = PlatformPost.save

    def fail_receipt_save(row, *args, **kwargs):
        if "platform_post_id" in (kwargs.get("update_fields") or []):
            raise RuntimeError("PRIVATE failure after accepted log insert")
        return original(row, *args, **kwargs)

    with engine_http(case, photo_response) as (engine, calls), patch.object(PlatformPost, "save", fail_receipt_save):
        result = engine._publish_platform_post(case[6])
    case[6].refresh_from_db()
    assert not result["success"] and case[6].status == "failed" and case[6].platform_post_id == ""
    assert case[6].retry_count == 0 and case[6].next_retry_at is None and len(calls) == 1
    assert case[6].publish_logs.filter(response_body__startswith='{"kind":"meta_publish_started"').count() == 1
    assert not case[6].publish_logs.filter(response_body__startswith='{"kind":"meta_publish_accepted"').exists()
