"""Accepted Meta receipts remain pending until native GET evidence is committed."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.core.files.base import ContentFile
from django.db import connection
from django.template.loader import render_to_string
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.organizations.models import Organization
from apps.publisher import meta_receipts
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import APIError
from providers.facebook import FacebookProvider
from providers.types import PublishState, PublishStatus


@pytest.fixture
def receipt_case(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    org = Organization.objects.create(name="Meta confirmation")
    workspace = Workspace.objects.create(organization=org, name="Meta workspace")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="111",
        account_name="Test Page",
        connection_status="connected",
        oauth_access_token="synthetic-page-grant",
    )
    post = Post.objects.create(workspace=workspace, caption="Owner approved caption")
    child = PlatformPost.objects.create(
        post=post,
        social_account=account,
        status="publishing",
        platform_post_id="",
        platform_extra={"post_type": "reel"},
    )
    provider = FacebookProvider({"page_id": "111"})
    provider.publish_post = MagicMock(side_effect=AssertionError("confirmation must never send a post"))
    asset = MediaAsset.objects.create(
        workspace=workspace,
        organization=org,
        file=ContentFile(b"ftyp-synthetic-test-video", name="video.mp4"),
        filename="video.mp4",
        media_type="video",
        processing_status="completed",
    )
    attachment = PostMedia.objects.create(post=post, media_asset=asset)
    case = SimpleNamespace(
        org=org,
        workspace=workspace,
        account=account,
        post=post,
        child=child,
        provider=provider,
        asset=asset,
        attachment=attachment,
    )
    admit(case)
    return case


def admit(case):
    case.child.refresh_from_db()
    case.child.platform_post_id = ""
    case.child.save(update_fields=["platform_post_id"])
    manifest = meta_receipts.prepared_manifest([case.attachment], [case.asset.file.path])
    digest = meta_receipts.binding(case.child, grant_kind="account", content_type="reel", manifest=manifest)
    case.context = meta_receipts.begin(
        case.child,
        expected_binding=digest,
        grant_kind="account",
        content_type="reel",
        manifest=manifest,
        native_resume=True,
    )
    result = {"meta_attempt": case.context, "platform_post_id": "555", "public_post_id": "555"}
    meta_receipts.accept(case.child, result, duration_ms=0)


def native_row(**changes):
    return {
        "id": "555",
        "from": {"id": "111"},
        "published": True,
        "status": {"video_status": "ready"},
        "permalink_url": "https://www.facebook.com/watch/?v=555",
        **changes,
    }


def sweep(case):
    with (
        patch("apps.publisher.engine.get_provider", return_value=case.provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value=case.provider.credentials),
        patch(
            "apps.publisher.engine._provider_and_access_token",
            side_effect=AssertionError("Meta confirmation must not refresh"),
        ),
    ):
        result = PublishEngine().confirm_pending_publishes()
    case.child.refresh_from_db()
    return result


@pytest.mark.django_db
def test_acceptance_does_not_mark_published(receipt_case):
    case = receipt_case
    engine = PublishEngine()
    accepted = {"success": True, "platform_post_id": "555", "async_publish": True, "response": {"video_id": "555"}}
    with patch.object(engine, "_dispatch_to_provider", return_value=accepted):
        assert engine._publish_platform_post(case.child)["success"] is True
    case.child.refresh_from_db()
    assert case.child.status == "publishing"
    assert case.child.published_at is None
    assert case.child.platform_post_id == "555"
    assert case.child.native_post_url == ""


@pytest.mark.django_db
def test_processing_then_ready_survives_new_engine_without_resend(receipt_case):
    case = receipt_case
    case.provider._request = MagicMock(
        side_effect=[
            httpx.Response(200, json=native_row(status={"video_status": "processing"})),
            httpx.Response(200, json=native_row()),
        ]
    )
    assert sweep(case) == 0
    assert case.child.status == "publishing" and case.child.published_at is None
    assert sweep(case) == 1
    assert case.child.status == "published" and case.child.published_at is not None
    assert case.child.native_post_url == "https://www.facebook.com/watch/?v=555"
    case.provider.publish_post.assert_not_called()
    assert all(call.args[0] == "GET" for call in case.provider._request.call_args_list)
    assert case.child.publish_logs.filter(response_body__contains="native_publish_confirmation").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["failed", "processing_timeout", "unknown_timeout", "unavailable"])
def test_failed_timeout_or_unavailable_never_resends(receipt_case, settings, mode):
    case = receipt_case
    settings.PUBLISHER_PUBLISH_CONFIRM_TIMEOUT = 5
    settings.PUBLISHER_UNCONFIRMED_TIMEOUT = 10
    if mode == "failed":
        row = native_row(status={"video_status": "error"})
    elif mode == "processing_timeout":
        row = native_row(status={"video_status": "processing"})
    else:
        row = native_row(permalink_url=None)
    case.provider._request = (
        MagicMock(side_effect=APIError("redacted", status_code=403))
        if mode == "unavailable"
        else MagicMock(return_value=httpx.Response(200, json=row))
    )
    PlatformPost.objects.filter(pk=case.child.pk).update(updated_at=timezone.now() - timedelta(seconds=30))
    sweep(case)
    assert case.child.status == "failed"
    assert case.child.platform_post_id == "555"
    assert case.child.next_retry_at is None
    assert case.child.native_post_url == ""
    assert not case.child.publish_logs.filter(response_body__contains="native_publish_confirmation").exists()
    case.provider.publish_post.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["owner", "grant", "caption", "media_owner", "hold", "handle", "author"])
def test_get_commit_race_cannot_promote_changed_receipt(receipt_case, mode):
    case = receipt_case
    asset = case.asset

    def confirm(*args):
        if mode == "owner":
            SocialAccount.objects.filter(pk=case.account.pk).update(account_platform_id="999")
        elif mode == "grant":
            case.account.oauth_access_token = "rotated-synthetic-grant"
            case.account.save()
        elif mode == "caption":
            Post.objects.filter(pk=case.post.pk).update(caption="Different approved copy")
        elif mode == "media_owner":
            foreign = Organization.objects.create(name="Foreign media")
            MediaAsset.objects.filter(pk=asset.pk).update(organization=foreign)
        elif mode == "hold":
            PlatformPost.objects.filter(pk=case.child.pk).update(status="on_hold")
        elif mode == "handle":
            PlatformPost.objects.filter(pk=case.child.pk).update(platform_post_id="666")
        else:
            user = User.objects.create_user(email="changed-meta-owner@example.com", password="testpass", name="Other")
            Post.objects.filter(pk=case.post.pk).update(author=user)
        return PublishStatus(
            PublishState.COMPLETE,
            platform_post_id="555",
            raw={"id": "555", "owner_id": "111", "permalink": "https://www.facebook.com/reel/555/"},
        )

    case.provider.check_publish_status = MagicMock(side_effect=confirm)
    assert sweep(case) == 0
    assert case.child.status != "published"
    assert case.child.published_at is None
    assert not case.child.publish_logs.filter(response_body__contains="native_publish_confirmation").exists()
    case.provider.publish_post.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["publishing", "published", "failed", "draft", "approved"])
def test_legacy_status_is_not_reconciled_or_promoted(receipt_case, state):
    case = receipt_case
    case.child.publish_logs.all().delete()
    PlatformPost.objects.filter(pk=case.child.pk).update(status=state)
    case.provider._request = MagicMock(side_effect=AssertionError("not pending"))
    assert sweep(case) == 0
    assert case.child.status == state
    assert case.child.native_post_url == ""
    case.provider._request.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_no_network_under_confirmation_row_lock(receipt_case):
    case = receipt_case

    def native_get(*args, **kwargs):
        assert connection.in_atomic_block is False
        return httpx.Response(200, json=native_row())

    case.provider._request = MagicMock(side_effect=native_get)
    assert sweep(case) == 1
    assert case.child.status == "published"


@pytest.mark.django_db
def test_view_post_uses_audited_native_link_and_legacy_is_disabled(receipt_case):
    case = receipt_case
    case.provider._request = MagicMock(return_value=httpx.Response(200, json=native_row()))
    assert sweep(case) == 1
    context = {"account": case.account, "post": case.child, "metric_tiles": []}
    html = render_to_string("analytics/_post_detail.html", context)
    assert 'href="https://www.facebook.com/watch/?v=555"' in html
    assert 'href="#"' not in html
    case.child.publish_logs.all().delete()
    html = render_to_string("analytics/_post_detail.html", context)
    assert "Native link not confirmed" in html
    assert 'href="#"' not in html
