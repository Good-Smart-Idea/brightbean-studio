"""Internal API approval uses real explicit capabilities and the existing workflow."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.api_keys.models import ApiKey, ApiKeyAuditLog
from apps.approvals.models import ApprovalAction, ApprovalReminder
from apps.composer.models import PlatformPost, Post
from apps.members.models import CustomRole, WorkspaceMembership
from apps.publisher import meta_receipts
from apps.publisher.models import PublishLog
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .test_submit_for_review import review_case as review_case


@pytest.fixture
def approval_case(review_case):
    key, child = review_case[3], review_case[6]
    key.permissions = ["create_posts", "approve_posts"]
    key.save(update_fields=["permissions"])
    child.status = "pending_review"
    child.save(update_fields=["status"])
    return review_case


def approve(case, *, reason="  Human authorized this selected revision.  ", post=None, **extra):
    return case[4].post(
        f"/api/v1/posts/{(post or case[5]).id}/approve",
        data={"reason": reason, **extra},
        content_type="application/json",
        secure=True,
    )


def sibling(case, state="pending_review", *, allow=True, workspace=None):
    account = SocialAccount.objects.create(
        workspace=workspace or case[1],
        platform="facebook",
        account_platform_id="approval-sibling",
        account_name="Sibling",
    )
    if allow:
        case[3].social_accounts.add(account)
    return PlatformPost.objects.create(post=case[5], social_account=account, status=state)


def assert_undecided(case):
    assert not ApprovalAction.objects.filter(post=case[5], action="approved").exists()
    case[6].refresh_from_db()
    assert case[6].status == "pending_review"


@pytest.mark.django_db
def test_approval_records_real_issuer_reason_and_preserves_content_history(approval_case):
    post, child = approval_case[5:]
    ApprovalAction.objects.create(post=post, user=approval_case[0], action="submitted", comment="Earlier review")
    before = (post.caption, post.title, post.first_comment, post.internal_notes, child.platform_extra)
    with patch("apps.api.routers.posts.transition_platform_post") as publish_transition:
        response = approve(approval_case)
        publish_transition.assert_not_called()
    assert response.status_code == 200, response.content
    post.refresh_from_db()
    child.refresh_from_db()
    assert child.status == "approved"
    assert (post.caption, post.title, post.first_comment, post.internal_notes, child.platform_extra) == before
    assert child.scheduled_at is None and child.published_at is None and not child.native_upload_state
    action = ApprovalAction.objects.get(post=post, action="approved")
    assert action.user == approval_case[0]
    assert action.comment == "Human authorized this selected revision."
    assert ApprovalAction.objects.filter(post=post, action="submitted").count() == 1
    assert (
        ApiKeyAuditLog.objects.filter(api_key=approval_case[3], action="post.approve", target_id=post.id).count() == 1
    )


@pytest.mark.django_db
def test_draft_must_use_existing_submission_before_internal_approval(approval_case):
    child = approval_case[6]
    child.status = "draft"
    child.save(update_fields=["status"])
    assert approve(approval_case).status_code == 409
    assert not ApprovalAction.objects.filter(post=approval_case[5]).exists()
    assert (
        approval_case[4].post(f"/api/v1/posts/{approval_case[5].id}/submit-for-review", secure=True).status_code == 200
    )
    assert approve(approval_case).status_code == 200
    assert ApprovalAction.objects.filter(post=approval_case[5], action="submitted").count() == 1


@pytest.mark.django_db
def test_all_approved_retry_is_idempotent_without_new_decision(approval_case):
    assert approve(approval_case).status_code == 200
    assert approve(approval_case, reason="Retry with a different explanation").status_code == 200
    assert ApprovalAction.objects.filter(post=approval_case[5], action="approved").count() == 1
    assert ApiKeyAuditLog.objects.filter(api_key=approval_case[3], action="post.approve.noop").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("permissions", [[], ["create_posts"], ["approve_posts"], ["publish_directly"]])
def test_explicit_key_needs_both_capabilities_even_for_owner(approval_case, permissions):
    key = approval_case[3]
    key.permissions = permissions
    key.save(update_fields=["permissions"])
    assert approve(approval_case).status_code == 403
    assert_undecided(approval_case)


@pytest.mark.django_db
@pytest.mark.parametrize("reason", ["", "  ", "x" * 2001])
def test_reason_must_be_nonblank_and_bounded(approval_case, reason):
    assert approve(approval_case, reason=reason).status_code == 422
    assert_undecided(approval_case)


@pytest.mark.django_db
def test_payload_cannot_override_actor_or_status(approval_case):
    assert approve(approval_case, status="scheduled", actor_id=str(approval_case[0].id)).status_code == 422
    assert_undecided(approval_case)


@pytest.mark.django_db
def test_foreign_author_requires_edit_others(approval_case):
    post = approval_case[5]
    post.author = User.objects.create_user(email="approve-other@example.com", password="testpass", name="Other")
    post.save(update_fields=["author"])
    assert approve(approval_case).status_code == 403
    approval_case[3].permissions += ["edit_others_posts"]
    approval_case[3].save(update_fields=["permissions"])
    assert approve(approval_case).status_code == 200


@pytest.mark.django_db
def test_foreign_workspace_is_hidden(approval_case):
    workspace = Workspace.objects.create(name="Other", organization=approval_case[1].organization)
    post = Post.objects.create(workspace=workspace, author=approval_case[0])
    assert approve(approval_case, post=post).status_code == 404
    assert_undecided(approval_case)


@pytest.mark.django_db
@pytest.mark.parametrize("cross_workspace", [False, True])
def test_all_children_must_match_workspace_and_explicit_allowlist(approval_case, cross_workspace):
    other = None
    if cross_workspace:
        other = Workspace.objects.create(name="Other", organization=approval_case[1].organization)
    sibling(approval_case, allow=cross_workspace, workspace=other)
    assert approve(approval_case).status_code == 404
    assert_undecided(approval_case)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [
        "draft",
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
def test_mixed_states_reject_whole_bundle(approval_case, state):
    sibling(approval_case, state)
    assert approve(approval_case).status_code == 409
    assert_undecided(approval_case)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("platform_post_id", "native-receipt"),
        ("published_at", timezone.now()),
        ("scheduled_at", timezone.now()),
        ("retry_count", 1),
        ("next_retry_at", timezone.now()),
        ("publish_error", "Earlier attempt"),
        ("native_upload_state", {"phase": "publishing"}),
        ("native_upload_state", {"phase": "started", "intent": "ambiguous"}),
        ("first_comment_status", "pending"),
        ("first_comment_id", "comment-receipt"),
        ("first_comment_error", "Earlier comment attempt"),
        ("first_comment_retry_count", 1),
        ("first_comment_posted_at", timezone.now()),
    ],
)
def test_publish_or_retry_evidence_is_never_adopted_or_cleared(approval_case, field, value):
    child = approval_case[6]
    setattr(child, field, value)
    child.save(update_fields=[field])
    assert approve(approval_case).status_code == 409
    child.refresh_from_db()
    assert getattr(child, field) == value
    assert_undecided(approval_case)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", [meta_receipts.STARTED, meta_receipts.ACCEPTED, meta_receipts.RECOVERED])
def test_clean_pending_review_cannot_adopt_a_typed_publish_ledger(approval_case, kind):
    account = approval_case[2]
    account.platform = "facebook"
    account.account_platform_id = "12345"
    account.save(update_fields=["platform", "account_platform_id"])
    marker = {
        "kind": kind,
        "attempt_id": str(uuid4()),
        "child_id": str(approval_case[6].id),
        "account_id": str(account.id),
        "workspace_id": str(approval_case[1].id),
        "owner_id": "12345",
        "platform": "facebook",
        "grant_kind": "account",
        "content_type": "reel",
        "binding": "a" * 64,
        "manifest": [],
        "media_bytes_verified": False,
    }
    if kind != meta_receipts.STARTED:
        marker.update(receipt_id="555", public_post_id="555")
    body = meta_receipts._encode(marker)
    log = PublishLog.objects.create(
        platform_post=approval_case[6], status_code=200 if kind == meta_receipts.ACCEPTED else None, response_body=body
    )
    with patch("apps.approvals.services.approve_post") as service:
        assert approve(approval_case).status_code == 409
        service.assert_not_called()
    log.refresh_from_db()
    assert log.response_body == body
    assert approval_case[6].publish_logs.count() == 1
    assert_undecided(approval_case)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        '{"kind":"meta_publish_started"}',
        '{"kind":"meta_publish_started","attempt_id":null}',
        '{"kind":"meta_publish_started",',
        '{"kind":"meta_publish_accepted","receipt_id":"555"}',
        '{"kind":"meta_publish_recovered","receipt_id":"555"}',
        '{ "unexpected": true, "kind": "meta_publish_started" }',
        "not JSON",
        "",
        "untyped provider failure",
    ],
)
def test_any_malformed_partial_or_untyped_ledger_blocks_unsent_approval(approval_case, body):
    log = PublishLog.objects.create(platform_post=approval_case[6], response_body=body)
    assert approve(approval_case).status_code == 409
    log.refresh_from_db()
    assert log.response_body == body
    assert_undecided(approval_case)


@pytest.mark.django_db
def test_history_on_one_child_blocks_every_target_without_changing_history(approval_case):
    other = sibling(approval_case)
    log = PublishLog.objects.create(platform_post=other, response_body='{"kind":"meta_publish_started"}')
    before = list(PublishLog.objects.filter(platform_post__post=approval_case[5]).values())
    assert approve(approval_case).status_code == 409
    assert set(approval_case[5].platform_posts.values_list("status", flat=True)) == {"pending_review"}
    assert list(PublishLog.objects.filter(platform_post__post=approval_case[5]).values()) == before
    assert PublishLog.objects.filter(id=log.id).exists()
    assert_undecided(approval_case)


@pytest.mark.django_db
def test_already_approved_noop_still_refuses_publish_history(approval_case):
    child = approval_case[6]
    child.status = "approved"
    child.save(update_fields=["status"])
    log = PublishLog.objects.create(platform_post=child, response_body='{"kind":"meta_publish_started"}')
    assert approve(approval_case).status_code == 409
    assert not ApprovalAction.objects.filter(post=approval_case[5]).exists()
    assert PublishLog.objects.filter(id=log.id).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["scheduled_at", "published_at"])
def test_parent_publish_evidence_blocks_approval(approval_case, field):
    post = approval_case[5]
    setattr(post, field, timezone.now())
    post.save(update_fields=[field])
    assert approve(approval_case).status_code == 409
    assert_undecided(approval_case)


@pytest.mark.django_db
def test_two_stage_internal_approval_preserves_client_stage_and_expected_notifications(approval_case):
    workspace = approval_case[1]
    workspace.approval_workflow_mode = "required_internal_and_client"
    workspace.save(update_fields=["approval_workflow_mode"])
    with patch("apps.approvals.services._notify_clients") as notify_clients:
        response = approve(approval_case)
        assert response.status_code == 200
        notify_clients.assert_called_once()
    approval_case[6].refresh_from_db()
    assert approval_case[6].status == "pending_client"
    assert response.json()["platform_posts"][0]["status"] == "pending_client"
    assert ApprovalReminder.objects.filter(post=approval_case[5], stage="pending_client").exists()
    assert approve(approval_case).status_code == 409
    assert ApprovalAction.objects.filter(post=approval_case[5], action="approved").count() == 1


@pytest.mark.django_db
def test_service_failure_rolls_back_every_target_and_mandatory_action(approval_case):
    from apps.approvals import services

    sibling(approval_case)
    original = services._record_action

    def fail_after_action(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("Synthetic approval failure")

    with (
        patch.object(services, "_record_action", side_effect=fail_after_action),
        pytest.raises(ValueError, match="Synthetic approval failure"),
    ):
        approve(approval_case)
    assert set(approval_case[5].platform_posts.values_list("status", flat=True)) == {"pending_review"}
    assert_undecided(approval_case)


@pytest.mark.django_db
def test_best_effort_request_audit_failure_does_not_erase_mandatory_decision(approval_case):
    with patch(
        "apps.api.middleware.ApiKeyAuditLog.objects.create", side_effect=RuntimeError("Synthetic audit failure")
    ):
        assert approve(approval_case).status_code == 200
    assert ApprovalAction.objects.filter(post=approval_case[5], action="approved").count() == 1


def mutate_after_lookup(case, mutation):
    from apps.api.routers import posts

    original = posts._get_workspace_post

    def changed(request, post_id):
        result = original(request, post_id)
        mutation()
        return result

    return patch.object(posts, "_get_workspace_post", side_effect=changed)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mutation",
    [
        "revoked",
        "expired",
        "permissions",
        "hash",
        "issuer",
        "inactive",
        "membership",
        "membership_role",
        "custom_role",
        "custom_role_permissions",
        "allowlist",
    ],
)
def test_cached_authenticated_grants_are_rechecked_under_lock(approval_case, mutation):
    user, workspace, account, key = approval_case[:4]
    if mutation == "custom_role_permissions":
        existing_role = CustomRole.objects.create(
            organization=workspace.organization,
            name="Approval role",
            permissions={"create_posts": True, "approve_posts": True},
        )
        WorkspaceMembership.objects.filter(user=user, workspace=workspace).update(custom_role=existing_role)
    # Populate verify_token's cache with the previously valid key first.
    assert approval_case[4].get(f"/api/v1/posts/{approval_case[5].id}", secure=True).status_code == 200

    def change():
        if mutation == "revoked":
            ApiKey.objects.filter(id=key.id).update(revoked_at=timezone.now())
        elif mutation == "expired":
            ApiKey.objects.filter(id=key.id).update(expires_at=timezone.now())
        elif mutation == "permissions":
            ApiKey.objects.filter(id=key.id).update(permissions=["create_posts"])
        elif mutation == "hash":
            ApiKey.objects.filter(id=key.id).update(token_hash="0" * 64)
        elif mutation == "issuer":
            ApiKey.objects.filter(id=key.id).update(issued_by=None)
        elif mutation == "inactive":
            User.objects.filter(id=user.id).update(is_active=False)
        elif mutation == "membership":
            WorkspaceMembership.objects.filter(user=user, workspace=workspace).delete()
        elif mutation == "membership_role":
            WorkspaceMembership.objects.filter(user=user, workspace=workspace).update(workspace_role="editor")
        elif mutation == "custom_role_permissions":
            CustomRole.objects.filter(id=existing_role.id).update(permissions={"create_posts": True})
        elif mutation == "custom_role":
            role = CustomRole.objects.create(
                organization=workspace.organization, name="No approvals", permissions={"create_posts": True}
            )
            WorkspaceMembership.objects.filter(user=user, workspace=workspace).update(custom_role=role)
        else:
            key.social_accounts.remove(account)

    with mutate_after_lookup(approval_case, change):
        assert approve(approval_case).status_code == (404 if mutation == "allowlist" else 403)
    assert_undecided(approval_case)


@pytest.mark.django_db
def test_post_deleted_after_initial_scope_lookup_is_hidden(approval_case):
    with mutate_after_lookup(approval_case, lambda: Post.objects.filter(id=approval_case[5].id).delete()):
        assert approve(approval_case).status_code == 404
    assert not ApprovalAction.objects.filter(post_id=approval_case[5].id).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("mutation", ["author", "workspace", "foreign_child", "workflow"])
def test_post_and_workflow_are_rechecked_under_lock(approval_case, mutation):
    def change():
        if mutation == "author":
            other = User.objects.create_user(email="approve-race@example.com", password="testpass", name="Other")
            Post.objects.filter(id=approval_case[5].id).update(author=other)
        elif mutation == "workspace":
            other = Workspace.objects.create(name="Other", organization=approval_case[1].organization)
            Post.objects.filter(id=approval_case[5].id).update(workspace=other)
        elif mutation == "foreign_child":
            sibling(approval_case, allow=False)
        else:
            Workspace.objects.filter(id=approval_case[1].id).update(
                approval_workflow_mode="required_internal_and_client"
            )

    with mutate_after_lookup(approval_case, change):
        response = approve(approval_case)
    if mutation == "workflow":
        assert response.status_code == 200
        approval_case[6].refresh_from_db()
        assert approval_case[6].status == "pending_client"
    else:
        assert response.status_code == (403 if mutation == "author" else 404)
        assert_undecided(approval_case)


def pg_thread(fn):
    close_old_connections()
    try:
        return fn()
    finally:
        close_old_connections()


def wait_for_pg_blocker(pid):
    """Prove a real database lock wait, rather than only a slow worker thread."""
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT cardinality(pg_blocking_pids(%s))", [pid])
            if cursor.fetchone()[0]:
                return
        sleep(0.01)
    pytest.fail("The competing PostgreSQL transaction never reached a real lock wait.")


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_approvals_record_one_decision(approval_case):
    if connection.vendor != "postgresql":
        pytest.skip("Real row-lock concurrency requires PostgreSQL.")
    from apps.approvals import services

    entered, release, second_started = Event(), Event(), Event()
    pids = {}
    original = services.approve_post

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10), "Approval synchronization timed out"
        return original(*args, **kwargs)

    def second_request():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pids["second"] = cursor.fetchone()[0]
        second_started.set()
        return approve(approval_case).status_code

    with patch.object(services, "approve_post", side_effect=paused), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pg_thread, lambda: approve(approval_case).status_code)
        try:
            assert entered.wait(10)
            second = pool.submit(pg_thread, second_request)
            assert second_started.wait(10)
            wait_for_pg_blocker(pids["second"])
        finally:
            release.set()
        assert first.result(timeout=15) == 200
        assert second.result(timeout=15) == 200
    assert ApprovalAction.objects.filter(post=approval_case[5], action="approved").count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mutation", ["child_insert", "scope_insert", "scope_delete", "ledger_insert"])
def test_postgres_parent_and_through_locks_serialize_scope_mutators(approval_case, mutation):
    if connection.vendor != "postgresql":
        pytest.skip("Real FK/through-row concurrency requires PostgreSQL.")
    from apps.approvals import services

    entered, release, mutator_started, mutator_done = Event(), Event(), Event(), Event()
    pids = {}
    original = services.approve_post
    extra_account = SocialAccount.objects.create(
        workspace=approval_case[1], platform="facebook", account_platform_id="concurrent", account_name="Concurrent"
    )

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    def mutate():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pids["mutator"] = cursor.fetchone()[0]
        mutator_started.set()
        with transaction.atomic():
            if mutation == "child_insert":
                PlatformPost.objects.create(post_id=approval_case[5].id, social_account=extra_account, status="draft")
            elif mutation == "scope_insert":
                ApiKey.objects.get(id=approval_case[3].id).social_accounts.add(extra_account)
            elif mutation == "ledger_insert":
                PublishLog.objects.create(
                    platform_post_id=approval_case[6].id, response_body='{"kind":"meta_publish_started"}'
                )
            else:
                ApiKey.objects.get(id=approval_case[3].id).social_accounts.remove(approval_case[2])
        mutator_done.set()

    with patch.object(services, "approve_post", side_effect=paused), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pg_thread, lambda: approve(approval_case).status_code)
        try:
            assert entered.wait(10)
            competing = pool.submit(pg_thread, mutate)
            assert mutator_started.wait(10)
            wait_for_pg_blocker(pids["mutator"])
            assert not mutator_done.is_set(), "Mutation committed while approval held its parent/scope locks"
        finally:
            release.set()
        assert first.result(timeout=15) == 200
        competing.result(timeout=15)
    assert mutator_done.is_set()
    assert ApprovalAction.objects.filter(post=approval_case[5], action="approved").count() == 1
