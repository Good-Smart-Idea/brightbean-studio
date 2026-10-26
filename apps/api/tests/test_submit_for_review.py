"""Approval-only REST transition: scoped, permission checked, retry safe."""

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.api_keys.services import issue_api_key
from apps.approvals.models import ApprovalAction
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def review_case(db):
    user = User.objects.create_user(
        email="review-api@example.com", password="testpass", name="Reviewer", tos_accepted_at=timezone.now()
    )
    org = Organization.objects.create(name="Review Org")
    workspace = Workspace.objects.create(
        name="Review Workspace", organization=org, approval_workflow_mode="required_internal"
    )
    OrgMembership.objects.create(user=user, organization=org, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="review-test",
        account_name="Review Account",
        connection_status="connected",
    )
    issued = issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=user, name="review", permissions=["create_posts"]
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")
    post = Post.objects.create(workspace=workspace, author=user, caption="Please review")
    child = PlatformPost.objects.create(post=post, social_account=account, status="draft")
    return user, workspace, account, issued.api_key, client, post, child


def submit(case, post=None):
    return case[4].post(f"/api/v1/posts/{(post or case[5]).id}/submit-for-review", secure=True)


@pytest.mark.django_db
def test_draft_submission_is_pending_review_without_publish_permission(review_case):
    response = submit(review_case)
    assert response.status_code == 200, response.content
    post, child = review_case[5:]
    post.refresh_from_db()
    child.refresh_from_db()
    assert child.status == "pending_review"
    assert child.scheduled_at is None
    assert post.scheduled_at is None
    assert child.published_at is None
    assert ApprovalAction.objects.filter(post=post, action="submitted").count() == 1


@pytest.mark.django_db
def test_pending_review_retry_does_not_duplicate_approval(review_case):
    assert submit(review_case).status_code == 200
    assert submit(review_case).status_code == 200
    assert ApprovalAction.objects.filter(post=review_case[5], action="submitted").count() == 1


@pytest.mark.django_db
def test_requires_create_posts_permission(review_case):
    key = review_case[3]
    key.permissions = []
    key.save(update_fields=["permissions"])
    assert submit(review_case).status_code == 403
    review_case[6].refresh_from_db()
    assert review_case[6].status == "draft"


@pytest.mark.django_db
def test_another_author_requires_edit_others_permission(review_case):
    other = User.objects.create_user(email="other-author@example.com", password="testpass", name="Other")
    post = review_case[5]
    post.author = other
    post.save(update_fields=["author"])
    assert submit(review_case).status_code == 403
    key = review_case[3]
    key.permissions = ["create_posts", "edit_others_posts"]
    key.save(update_fields=["permissions"])
    assert submit(review_case).status_code == 200


@pytest.mark.django_db
def test_foreign_workspace_is_hidden(review_case):
    other_workspace = Workspace.objects.create(name="Other", organization=review_case[1].organization)
    post = Post.objects.create(workspace=other_workspace, author=review_case[0], caption="Foreign")
    assert submit(review_case, post).status_code == 404


@pytest.mark.django_db
def test_non_allowlisted_child_prevents_whole_submission(review_case):
    account = SocialAccount.objects.create(
        workspace=review_case[1], platform="facebook", account_platform_id="not-allowed", account_name="Other Account"
    )
    PlatformPost.objects.create(post=review_case[5], social_account=account, status="draft")
    assert submit(review_case).status_code == 404
    assert set(review_case[5].platform_posts.values_list("status", flat=True)) == {"draft"}
    assert not ApprovalAction.objects.filter(post=review_case[5]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [
        "scheduled",
        "publishing",
        "published",
        "approved",
        "pending_client",
        "changes_requested",
        "rejected",
        "on_hold",
        "failed",
    ],
)
def test_other_states_are_rejected_without_transition(review_case, state):
    child = review_case[6]
    child.status = state
    child.save(update_fields=["status"])
    assert submit(review_case).status_code == 409
    child.refresh_from_db()
    assert child.status == state
    assert not ApprovalAction.objects.filter(post=review_case[5]).exists()
