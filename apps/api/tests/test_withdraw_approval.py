"""Approval withdrawal cannot reset or conceal an attempted publication."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.approvals.models import ApprovalAction
from apps.approvals.services import withdraw_approval
from apps.composer.models import PlatformPost, Post
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .test_submit_for_review import review_case as review_case


@pytest.fixture
def approved_case(review_case):
    case = review_case
    key, child = case[3], case[6]
    key.permissions = ["create_posts", "approve_posts"]
    key.save(update_fields=["permissions"])
    child.status = "approved"
    child.save(update_fields=["status"])
    return case


def withdraw(case, *, reason="  Duplicate of confirmed published post.  ", post=None):
    return case[4].post(
        f"/api/v1/posts/{(post or case[5]).id}/withdraw-approval",
        data={"reason": reason},
        content_type="application/json",
        secure=True,
    )


def sibling(case, state="draft", allow=True):
    account = SocialAccount.objects.create(
        workspace=case[1], platform="facebook", account_platform_id="withdraw-approval-sibling", account_name="Sibling"
    )
    if allow:
        case[3].social_accounts.add(account)
    return PlatformPost.objects.create(post=case[5], social_account=account, status=state)


@pytest.mark.django_db
def test_explicit_withdrawal_preserves_content_and_prior_approval_audit(approved_case):
    post, child = approved_case[5:]
    ApprovalAction.objects.create(post=post, user=approved_case[0], action="approved", comment="Owner approval")
    assert withdraw(approved_case).status_code == 200
    assert withdraw(approved_case).status_code == 200
    child.refresh_from_db()
    post.refresh_from_db()
    assert child.status == "draft" and child.scheduled_at is None and child.published_at is None
    assert post.caption == "Please review"
    assert ApprovalAction.objects.filter(post=post, action="approved").count() == 1
    action = ApprovalAction.objects.get(post=post, action="withdrawn")
    assert action.user == approved_case[0]
    assert action.comment == "Approval withdrawn: Duplicate of confirmed published post."


@pytest.mark.django_db
@pytest.mark.parametrize("permissions", [[], ["create_posts"], ["approve_posts"]])
def test_requires_edit_and_approval_permissions(approved_case, permissions):
    key = approved_case[3]
    key.permissions = permissions
    key.save(update_fields=["permissions"])
    assert withdraw(approved_case).status_code == 403
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("reason", ["", "  ", "x" * 2001])
def test_requires_bounded_nonblank_reason(approved_case, reason):
    assert withdraw(approved_case, reason=reason).status_code == 422
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
def test_foreign_author_requires_edit_others(approved_case):
    post = approved_case[5]
    post.author = User.objects.create_user(email="approval-other@example.com", password="testpass", name="Other")
    post.save(update_fields=["author"])
    assert withdraw(approved_case).status_code == 403
    key = approved_case[3]
    key.permissions += ["edit_others_posts"]
    key.save(update_fields=["permissions"])
    assert withdraw(approved_case).status_code == 200


@pytest.mark.django_db
def test_foreign_workspace_is_hidden(approved_case):
    workspace = Workspace.objects.create(name="Other", organization=approved_case[1].organization)
    post = Post.objects.create(workspace=workspace, author=approved_case[0])
    assert withdraw(approved_case, post=post).status_code == 404


@pytest.mark.django_db
def test_foreign_child_blocks_every_target(approved_case):
    sibling(approved_case, "approved", allow=False)
    assert withdraw(approved_case).status_code == 404
    assert set(approved_case[5].platform_posts.values_list("status", flat=True)) == {"approved"}
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [
        "pending_review",
        "pending_client",
        "scheduled",
        "publishing",
        "published",
        "failed",
        "on_hold",
        "rejected",
        "changes_requested",
    ],
)
def test_mixed_nonapproval_states_fail_atomically_before_audit(approved_case, state):
    sibling(approved_case, state)
    with patch("apps.api.routers.posts.log_audit_entry") as audit:
        assert withdraw(approved_case).status_code == 409
        audit.assert_not_called()
    approved_case[6].refresh_from_db()
    assert approved_case[6].status == "approved"
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [("platform_post_id", "native-receipt"), ("published_at", timezone.now()), ("scheduled_at", timezone.now())],
)
def test_provider_or_publish_markers_block_reset(approved_case, field, value):
    child = approved_case[6]
    setattr(child, field, value)
    child.save(update_fields=[field])
    assert withdraw(approved_case).status_code == 409
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["published_at", "scheduled_at"])
def test_parent_publish_marker_blocks_reset(approved_case, field):
    post = approved_case[5]
    setattr(post, field, timezone.now())
    post.save(update_fields=[field])
    assert withdraw(approved_case).status_code == 409
    assert not ApprovalAction.objects.filter(post=post).exists()


@pytest.mark.django_db
def test_scope_is_rechecked_after_initial_lookup(approved_case):
    from apps.api.routers import posts

    original = posts._get_workspace_post

    def add_foreign_after_lookup(request, post_id):
        post = original(request, post_id)
        sibling(approved_case, "approved", allow=False)
        return post

    with patch.object(posts, "_get_workspace_post", side_effect=add_foreign_after_lookup):
        assert withdraw(approved_case).status_code == 404
    assert set(approved_case[5].platform_posts.values_list("status", flat=True)) == {"approved"}
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
def test_author_is_rechecked_under_lock(approved_case):
    from apps.api.routers import posts

    original = posts._get_workspace_post
    other = User.objects.create_user(email="approval-race@example.com", password="testpass", name="Other")

    def change_author_after_lookup(request, post_id):
        post = original(request, post_id)
        Post.objects.filter(id=post.id).update(author=other)
        return post

    with patch.object(posts, "_get_workspace_post", side_effect=change_author_after_lookup):
        assert withdraw(approved_case).status_code == 403
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
def test_stale_service_instance_cannot_reset_published_child(approved_case):
    child = approved_case[6]
    PlatformPost.objects.filter(id=child.id).update(status="published", platform_post_id="native")
    with pytest.raises(ValueError, match="Only unsent"):
        withdraw_approval(approved_case[5], approved_case[0], approved_case[1], "Duplicate")
    child.refresh_from_db()
    assert child.status == "published" and child.platform_post_id == "native"
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
def test_service_failure_rolls_back_all_targets_and_audit(approved_case):
    from apps.approvals import services

    sibling(approved_case, "approved")
    transition = services._transition_or_skip
    count = 0

    def fail_second(target, state):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("Synthetic transition failure")
        return transition(target, state)

    with patch.object(services, "_transition_or_skip", side_effect=fail_second):
        assert withdraw(approved_case).status_code == 409
    assert set(approved_case[5].platform_posts.values_list("status", flat=True)) == {"approved"}
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()


@pytest.mark.django_db
def test_approved_and_draft_siblings_are_withdrawn_without_sending(approved_case):
    sibling(approved_case, "draft")
    assert withdraw(approved_case).status_code == 200
    assert set(approved_case[5].platform_posts.values_list("status", flat=True)) == {"draft"}


@pytest.mark.django_db
def test_workspace_is_rechecked_under_lock(approved_case):
    from apps.api.routers import posts

    original = posts._get_workspace_post
    other = Workspace.objects.create(name="Changed", organization=approved_case[1].organization)

    def change_workspace_after_lookup(request, post_id):
        post = original(request, post_id)
        Post.objects.filter(id=post.id).update(workspace=other)
        return post

    with patch.object(posts, "_get_workspace_post", side_effect=change_workspace_after_lookup):
        assert withdraw(approved_case).status_code == 404
    assert not ApprovalAction.objects.filter(post=approved_case[5]).exists()
