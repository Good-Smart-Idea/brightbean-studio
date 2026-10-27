"""API scheduling respects final approval and all-target atomicity."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost
from apps.social_accounts.models import SocialAccount

from .test_submit_for_review import review_case as review_case


@pytest.fixture(params=["rest", "mcp"])
def schedule_call(request, review_case):
    key = review_case[3]
    key.permissions = ["create_posts", "publish_directly"]
    key.save(update_fields=["permissions"])
    when = timezone.now() + timedelta(days=1)

    def call():
        client, post = review_case[4:6]
        args = {"scheduled_at": when.isoformat()}
        if request.param == "rest":
            response = client.post(
                f"/api/v1/posts/{post.id}/schedule", data=json.dumps(args), content_type="application/json", secure=True
            )
            return response.status_code == 200
        args["post_id"] = str(post.id)
        response = client.post(
            "/api/v1/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "schedule_draft", "arguments": args},
                }
            ),
            content_type="application/json",
            secure=True,
        )
        data = response.json()
        return response.status_code == 200 and "error" not in data and not data.get("result", {}).get("isError", False)

    return call, when


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["required_internal", "required_internal_and_client"])
def test_final_approval_schedules_and_retry_preserves_state(review_case, schedule_call, mode):
    workspace = review_case[1]
    workspace.approval_workflow_mode = mode
    workspace.save(update_fields=["approval_workflow_mode"])
    child = review_case[6]
    child.status = "approved"
    child.save(update_fields=["status"])
    call, when = schedule_call
    assert call()
    child.refresh_from_db()
    assert child.status == "scheduled"
    assert child.scheduled_at == when
    before = child.updated_at
    assert call()
    child.refresh_from_db()
    assert child.updated_at == before


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["draft", "pending_review", "pending_client", "published"])
def test_unapproved_or_noneligible_is_not_scheduled(review_case, schedule_call, state):
    child = review_case[6]
    child.status = state
    child.save(update_fields=["status"])
    assert not schedule_call[0]()
    child.refresh_from_db()
    assert child.status == state
    assert child.scheduled_at is None


@pytest.mark.django_db
def test_mixed_state_fails_without_scheduling_approved_child(review_case, schedule_call):
    child = review_case[6]
    child.status = "approved"
    child.save(update_fields=["status"])
    account = SocialAccount.objects.create(
        workspace=review_case[1], platform="facebook", account_platform_id="second", account_name="Second"
    )
    review_case[3].social_accounts.add(account)
    PlatformPost.objects.create(post=review_case[5], social_account=account, status="pending_client")
    assert not schedule_call[0]()
    child.refresh_from_db()
    assert child.status == "approved"


@pytest.mark.django_db
def test_approved_requires_edit_others_and_publish_permission(review_case, schedule_call):
    post, child = review_case[5:]
    child.status = "approved"
    child.save(update_fields=["status"])
    post.author = User.objects.create_user(email="different@example.com", name="Other")
    post.save(update_fields=["author"])
    assert not schedule_call[0]()
    key = review_case[3]
    key.permissions = ["create_posts", "edit_others_posts"]
    key.save(update_fields=["permissions"])
    assert not schedule_call[0]()
    child.refresh_from_db()
    assert child.status == "approved"


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["scope", "media", "quota"])
def test_approved_preserves_account_scope_media_and_quota(review_case, schedule_call, monkeypatch, failure):
    child = review_case[6]
    child.status = "approved"
    child.save(update_fields=["status"])
    if failure == "scope":
        review_case[3].social_accounts.clear()
    elif failure == "media":
        account = review_case[2]
        account.platform = "instagram"
        account.save(update_fields=["platform"])
    else:
        from ninja.errors import HttpError

        def reject(*args):
            raise HttpError(429, "Quota exceeded")

        monkeypatch.setattr("apps.api.routers.posts.check_platform_quota", reject)
        monkeypatch.setattr("apps.mcp.handlers.check_platform_quota", reject)
    assert not schedule_call[0]()
    child.refresh_from_db()
    assert child.status == "approved"
