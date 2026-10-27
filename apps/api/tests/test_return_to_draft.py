"""Review withdrawal is scoped, atomic, audited, and cannot unpublish."""

import pytest

from apps.accounts.models import User
from apps.approvals.models import ApprovalAction, ApprovalReminder
from apps.approvals.services import withdraw_review
from apps.composer.models import PlatformPost, Post
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .test_submit_for_review import review_case as review_case


def sibling_account(case):
    account = SocialAccount.objects.create(
        workspace=case[1], platform="facebook", account_platform_id="sibling-withdraw", account_name="Sibling"
    )
    case[3].social_accounts.add(account)
    return account


def withdraw(case, post=None):
    return case[4].post(f"/api/v1/posts/{(post or case[5]).id}/return-to-draft", secure=True)


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["pending_review", "pending_client"])
def test_withdraws_review_and_cleans_reminder_retry_safely(review_case, state):
    post, child = review_case[5:]
    child.status = state
    child.save(update_fields=["status"])
    ApprovalReminder.objects.create(post=post, stage=state)
    assert withdraw(review_case).status_code == 200
    assert withdraw(review_case).status_code == 200
    child.refresh_from_db()
    post.refresh_from_db()
    assert child.status == "draft"
    assert child.scheduled_at is None and post.scheduled_at is None
    assert child.published_at is None
    assert ApprovalAction.objects.filter(post=post, action="withdrawn").count() == 1
    assert not ApprovalReminder.objects.filter(post=post).exists()


@pytest.mark.django_db
def test_requires_create_posts(review_case):
    key = review_case[3]
    key.permissions = []
    key.save(update_fields=["permissions"])
    assert withdraw(review_case).status_code == 403


@pytest.mark.django_db
def test_other_author_needs_edit_others(review_case):
    post = review_case[5]
    post.author = User.objects.create_user(email="withdraw-other@example.com", password="testpass", name="Other")
    post.save(update_fields=["author"])
    assert withdraw(review_case).status_code == 403
    key = review_case[3]
    key.permissions = ["create_posts", "edit_others_posts"]
    key.save(update_fields=["permissions"])
    assert withdraw(review_case).status_code == 200


@pytest.mark.django_db
def test_foreign_workspace_hidden(review_case):
    workspace = Workspace.objects.create(name="Foreign", organization=review_case[1].organization)
    post = Post.objects.create(workspace=workspace, author=review_case[0])
    assert withdraw(review_case, post).status_code == 404


@pytest.mark.django_db
def test_foreign_account_rejects_whole_operation(review_case):
    account = SocialAccount.objects.create(
        workspace=review_case[1], platform="facebook", account_platform_id="foreign-withdraw", account_name="Foreign"
    )
    child = review_case[6]
    child.status = "pending_review"
    child.save(update_fields=["status"])
    PlatformPost.objects.create(post=review_case[5], social_account=account, status="pending_review")
    assert withdraw(review_case).status_code == 404
    assert set(review_case[5].platform_posts.values_list("status", flat=True)) == {"pending_review"}
    assert not ApprovalAction.objects.filter(post=review_case[5]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["scheduled", "publishing", "published", "approved", "failed", "rejected", "on_hold"])
def test_non_review_sibling_blocks_all_changes(review_case, state):
    child = review_case[6]
    child.status = "pending_review"
    child.save(update_fields=["status"])
    sibling = PlatformPost.objects.create(
        post=review_case[5], social_account=sibling_account(review_case), status=state
    )
    response = withdraw(review_case)
    assert response.status_code == 409
    child.refresh_from_db()
    sibling.refresh_from_db()
    assert child.status == "pending_review" and sibling.status == state
    assert not ApprovalAction.objects.filter(post=review_case[5]).exists()


@pytest.mark.django_db
def test_single_account_withdrawal_keeps_sibling_reminder(review_case):
    post, child = review_case[5:]
    child.status = "pending_review"
    child.save(update_fields=["status"])
    PlatformPost.objects.create(post=post, social_account=sibling_account(review_case), status="pending_review")
    ApprovalReminder.objects.create(post=post, stage="pending_review")
    withdraw_review(child, review_case[0], review_case[1])
    assert ApprovalReminder.objects.filter(post=post, stage="pending_review").exists()
    assert ApprovalAction.objects.filter(post=post, platform_post=child, action="withdrawn").exists()
