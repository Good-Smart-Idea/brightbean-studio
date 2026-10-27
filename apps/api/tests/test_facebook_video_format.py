"""A Facebook-only format edit cannot send, reapprove, or mutate sibling posts."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .test_submit_for_review import review_case as review_case


@pytest.fixture
def video_case(review_case):
    account = review_case[2]
    account.platform = "facebook"
    account.save(update_fields=["platform"])
    asset = MediaAsset.objects.create(
        workspace=review_case[1],
        organization=review_case[1].organization,
        file="test/reaction.mp4",
        filename="reaction.mp4",
        media_type="video",
        duration=54.5,
        processing_status="completed",
    )
    PostMedia.objects.create(post=review_case[5], media_asset=asset, position=0)
    return review_case, asset


def endpoint(case, child=None, post=None):
    return f"/api/v1/posts/{(post or case[5]).id}/platform-posts/{(child or case[6]).id}/facebook-video-format"


def update(case, value="reel", **kwargs):
    return case[4].patch(
        endpoint(case, **kwargs), data={"publish_as": value}, content_type="application/json", secure=True
    )


def sibling(case, *, allow=True, status="published"):
    account = SocialAccount.objects.create(
        workspace=case[1], platform="youtube", account_platform_id="format-sibling", account_name="YT"
    )
    if allow:
        case[3].social_accounts.add(account)
    return PlatformPost.objects.create(
        post=case[5], social_account=account, status=status, platform_post_id="yt-receipt"
    )


@pytest.mark.django_db
def test_read_reports_default_and_reel_without_generic_extra(video_case):
    case, _ = video_case
    case[6].platform_extra = {"post_type": "reel", "provider_secret": "must-not-return"}
    case[6].save(update_fields=["platform_extra"])
    response = case[4].get(endpoint(case), secure=True)
    assert response.status_code == 200
    assert response.json()["configured_publish_as"] == "reel"
    assert response.json()["resolved_post_type"] == "reel"
    assert "must-not-return" not in response.content.decode()


@pytest.mark.django_db
def test_reel_and_regular_round_trip_preserve_other_extras_and_state(video_case):
    case, _ = video_case
    case[6].platform_extra = {"audience": "public"}
    case[6].save(update_fields=["platform_extra"])
    assert update(case).json()["resolved_post_type"] == "reel"
    assert update(case).status_code == 200
    case[6].refresh_from_db()
    assert case[6].platform_extra == {"audience": "public", "post_type": "reel"}
    assert update(case, "video").json()["resolved_post_type"] == "video"
    case[6].refresh_from_db()
    assert case[6].platform_extra == {"audience": "public"}
    assert case[6].status == "draft"
    assert case[6].scheduled_at is None and case[6].published_at is None and not case[6].platform_post_id


@pytest.mark.django_db
def test_published_sibling_is_never_changed_or_returned(video_case):
    case, _ = video_case
    other = sibling(case)
    before = PlatformPost.objects.filter(id=other.id).values().get()
    result = update(case)
    assert result.status_code == 200
    assert result.json()["platform_post_id"] == str(case[6].id)
    assert str(other.id) not in result.content.decode()
    assert PlatformPost.objects.filter(id=other.id).values().get() == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [
        "approved",
        "pending_review",
        "pending_client",
        "scheduled",
        "publishing",
        "published",
        "failed",
        "on_hold",
        "changes_requested",
        "rejected",
    ],
)
def test_only_draft_target_is_editable(video_case, state):
    case, _ = video_case
    case[6].status = state
    case[6].save(update_fields=["status"])
    assert update(case).status_code == 409
    case[6].refresh_from_db()
    assert case[6].platform_extra == {}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("platform_post_id", "native"),
        ("published_at", timezone.now()),
        ("scheduled_at", timezone.now()),
        ("next_retry_at", timezone.now()),
        ("retry_count", 1),
    ],
)
def test_attempt_markers_block_disguised_draft(video_case, field, value):
    case, _ = video_case
    setattr(case[6], field, value)
    case[6].save(update_fields=[field])
    assert update(case).status_code == 409


@pytest.mark.django_db
def test_foreign_child_scope_blocks_read_and_write(video_case):
    case, _ = video_case
    sibling(case, allow=False)
    assert case[4].get(endpoint(case), secure=True).status_code == 404
    assert update(case).status_code == 404


@pytest.mark.django_db
def test_non_facebook_child_and_foreign_post_child_are_hidden(video_case):
    case, _ = video_case
    other = sibling(case)
    assert update(case, child=other).status_code == 404
    post = Post.objects.create(workspace=case[1], author=case[0])
    alien = PlatformPost.objects.create(post=post, social_account=case[2])
    assert update(case, child=alien).status_code == 404


@pytest.mark.django_db
def test_permission_and_foreign_author_checks(video_case):
    case, _ = video_case
    key = case[3]
    key.permissions = []
    key.save(update_fields=["permissions"])
    assert update(case).status_code == 403
    key.permissions = ["create_posts"]
    key.save(update_fields=["permissions"])
    case[5].author = User.objects.create_user(email="format-author@example.com", password="testpass", name="Other")
    case[5].save(update_fields=["author"])
    assert update(case).status_code == 403
    key.permissions += ["edit_others_posts"]
    key.save(update_fields=["permissions"])
    assert update(case).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["none", "two", "image", "pending", "processing", "failed", "no_file", "foreign"])
def test_invalid_media_is_rejected_without_changes(video_case, kind):
    case, asset = video_case
    if kind == "none":
        case[5].media_attachments.all().delete()
    elif kind == "two":
        other = MediaAsset.objects.create(
            workspace=case[1], media_type="video", file="test/other.mp4", filename="other.mp4"
        )
        PostMedia.objects.create(post=case[5], media_asset=other, position=1)
    elif kind == "image":
        asset.media_type = "image"
        asset.save(update_fields=["media_type"])
    elif kind == "no_file":
        asset.file = ""
        asset.save(update_fields=["file"])
    elif kind == "foreign":
        asset.workspace = Workspace.objects.create(name="Other", organization=case[1].organization)
        asset.save(update_fields=["workspace"])
    else:
        asset.processing_status = kind
        asset.save(update_fields=["processing_status"])
    assert update(case).status_code == (404 if kind == "foreign" else 422)
    case[6].refresh_from_db()
    assert case[6].platform_extra == {}


@pytest.mark.django_db
@pytest.mark.parametrize("duration,expected", [(2.9, 422), (90.1, 422), (3, 200), (90, 200), (0, 200)])
def test_reel_duration_boundary_and_unknown(video_case, duration, expected):
    case, asset = video_case
    asset.duration = duration
    asset.save(update_fields=["duration"])
    assert update(case).status_code == expected


@pytest.mark.django_db
def test_regular_video_has_no_reel_duration_limit(video_case):
    case, asset = video_case
    asset.duration = 500
    asset.save(update_fields=["duration"])
    assert update(case, "video").status_code == 200


@pytest.mark.django_db
def test_arbitrary_provider_fields_and_invalid_format_are_rejected(video_case):
    case, _ = video_case
    assert update(case, "story").status_code == 422
    result = case[4].patch(
        endpoint(case),
        data={"publish_as": "reel", "platform_extra": {"page_id": "other"}},
        content_type="application/json",
        secure=True,
    )
    assert result.status_code == 422


@pytest.mark.django_db
@pytest.mark.parametrize("race", ["status", "scope", "workspace", "author"])
def test_scope_and_editability_are_rechecked_under_lock(video_case, race):
    from apps.api.routers import posts

    case, _ = video_case
    original = posts._get_workspace_post

    def change_after_lookup(request, post_id):
        post = original(request, post_id)
        if race == "status":
            PlatformPost.objects.filter(id=case[6].id).update(status="publishing")
        elif race == "scope":
            sibling(case, allow=False)
        elif race == "workspace":
            other = Workspace.objects.create(name="Race", organization=case[1].organization)
            Post.objects.filter(id=post_id).update(workspace=other)
        else:
            other = User.objects.create_user(email="format-race@example.com", password="testpass", name="Other")
            Post.objects.filter(id=post_id).update(author=other)
        return post

    with patch.object(posts, "_get_workspace_post", side_effect=change_after_lookup):
        assert update(case).status_code == {"status": 409, "scope": 404, "workspace": 404, "author": 403}[race]
    case[6].refresh_from_db()
    assert case[6].platform_extra == {}


@pytest.mark.django_db
def test_read_resolves_stale_hint_against_actual_media(video_case):
    case, asset = video_case
    asset.media_type = "image"
    asset.save(update_fields=["media_type"])
    case[6].platform_extra = {"post_type": "reel"}
    case[6].save(update_fields=["platform_extra"])
    response = case[4].get(endpoint(case), secure=True)
    assert response.status_code == 200
    assert response.json()["configured_publish_as"] == "reel"
    assert response.json()["resolved_post_type"] == "image"


@pytest.mark.django_db
def test_org_shared_video_remains_supported(video_case):
    case, asset = video_case
    asset.workspace = None
    asset.save(update_fields=["workspace"])
    assert update(case).status_code == 200


@pytest.mark.django_db
def test_audit_failure_rolls_back_format_change(video_case):
    case, _ = video_case
    with (
        patch("apps.api.routers.posts.log_audit_entry", side_effect=RuntimeError("Synthetic audit failure")),
        pytest.raises(RuntimeError, match="Synthetic audit"),
    ):
        update(case)
    case[6].refresh_from_db()
    assert case[6].platform_extra == {}


@pytest.mark.django_db
def test_authorized_update_has_child_specific_audit(video_case):
    case, _ = video_case
    with patch("apps.api.routers.posts.log_audit_entry") as audit:
        assert update(case).status_code == 200
    assert audit.call_args.kwargs["action"] == "post.facebook_video_format.update"
    assert audit.call_args.kwargs["target_id"] == case[6].id
