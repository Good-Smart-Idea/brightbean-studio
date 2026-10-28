# Agent REST internal approval — source design

Status: SOURCE IMPLEMENTED FOR REVIEW, based on reviewed BrightBean main 4fda74638a5fe31f117d97afbd5a856e544126bd. Synthetic offline validation is recorded below. No public CI, deployment, live approval, scheduling or provider request has occurred.

## Problem and authorized boundary

The Agent API can submit a draft and withdraw an approval, but cannot approve a submitted post. After a technical media/format correction returned the selected post to draft, scheduling correctly refuses the required-internal-review workspace. Approval must go through the existing workflow using the API, preserving the direct human instruction to use the API strictly. The endpoint must not disable that workspace gate, set statuses directly, manufacture a human review, or bypass client approval.

This feature supplies an approval-capable API operation. Authorization for an actual decision remains specific to the selected content and human instruction. The database actor is honestly the authenticated API key issuer; a reason can record the supplied context but never assert that a different person performed the API operation.

## Existing capability and actor facts

- `apps/api_keys/models.py::ApiKey.permissions` is a persisted JSON list of explicit key grants. It is not derived automatically from the issuer being an owner/admin. `social_accounts` is a persisted explicit M2M allowlist, and `workspace` is a single FK.
- `apps/members/models.py::PERMISSION_KEYS` already includes `create_posts`, `approve_posts` and `edit_others_posts`. No new permission, model, migration, credential or automatic scope expansion is needed for this design.
- `apps/api_keys/services.py::issue_api_key` stores the requested permission set only after checking the issuer has org `manage_api_keys`, workspace membership, and every requested permission. Account targets must belong to that workspace.
- `apps/api/auth.py::_resolve_effective_permissions` intersects the stored key permissions with the issuer's current workspace effective permissions. A custom role supplies its explicit permission map; otherwise the built-in workspace role does. Owner/admin identity alone cannot grant an operation absent from the key list.
- `ApiKeyAuth` sets `request.user` to the key's `issued_by`, and a virtual membership carries that intersection. `verify_token` rejects revoked/expired keys and deleted/offboarded issuers. A short auth cache exists, so this sensitive transition must refetch/recheck the real key and membership under transaction locks rather than trust a pre-lock cached object.
- `approve_posts` alone is insufficient: this route also requires `create_posts`. A client role has approval permission but lacks create permission by default; it cannot use this internal approval operation.
- The account allowlist is an Agent API resource boundary, not proof of a valid provider access token or native publishing eligibility. Approval makes no provider readiness claim and performs no provider call.

## Proposed route and payload

`POST /api/v1/posts/{post_id}/approve`

Payload: `{ "reason": "bounded decision context" }`. Require a trimmed, nonblank reason of 1–2000 characters; forbid extra input fields. The response is the existing PostResponse reflecting actual child states, not an unconditional approved/success label.

Enforce existing write-rate limits and `create_posts` plus `approve_posts`. Require `edit_others_posts` when the locked post's actual author differs from the real actor. No generic status, actor/user override, account expansion, client-finalization option or scheduling parameter is accepted.

## Transaction and fresh authorization

1. Use the existing scoped lookup to hide missing, foreign-workspace or partially out-of-allowlist posts as 404.
2. In one atomic transaction, refetch and lock the real ApiKey, issuer membership/custom role, workspace, post, all PlatformPost children, involved SocialAccounts, and relevant persisted allowlist rows. Use explicit stable ordering and avoid nullable joined-row FOR UPDATE queries. Recheck key active state, original authenticated issuer binding, membership existence, explicit-key/current-member permission intersection, workspace identity/mode, author permission and every child/account workspace and allowlist.
3. Reject no-child posts. Permit child states only pending_review or already approved. Any draft, changes_requested, rejected, pending_client, on_hold, scheduled, publishing, published or failed sibling rejects the entire bundle before transition/audit decision.
4. Reject parent/child schedule or publication timestamps, native platform IDs, retry count/next retry, publish errors, nonempty native_upload_state (which includes durable native intent/checkpoints), and first-comment attempt/receipt markers. Approval cannot reset, adopt or hide an attempted publication.
5. If every child is already approved and all safety/authorization checks still pass, return an idempotent current-state response without invoking the service or creating another ApprovalAction.
6. If any child is pending_review, call existing `apps.approvals.services.approve_post(post, actual_actor, locked_workspace, trimmed_reason)`. Do not assign status fields directly. Keep its mandatory ApprovalAction in the same atomic transaction; service failure rolls back all child and decision changes.
7. Record the API operation through existing log_audit_entry with post.approve or post.approve.noop. This helper is best effort; the service's ApprovalAction is the mandatory durable decision record, independent of that optional request-audit row. Response reflects refreshed current states. No body/secrets are added to request-audit storage.

Scope/permission mutations occurring before the locked recheck must be refused. Concurrent approval calls serialize on the same rows and create one decision. The focused PostgreSQL test must prove actual row-lock behavior; SQLite or mocked races cannot establish that claim.

## Workflow and two-stage behavior

A draft must first use the normal submit-for-review API. The new route never calls the service's broader draft/rejected/client approval allowance. In required_internal mode, pending_review becomes approved. In required_internal_and_client mode, the existing service advances internal review to pending_client and preserves client reminders/notification behavior. The response must show pending_client; repeating this internal route refuses that state and never finalizes client approval or schedules a post.

Approval preserves caption, media, target list, format metadata, approval history and all publication-related fields. It neither schedules nor consumes the authorized once-only provider attempt. Scheduling and native verification remain separate explicit steps.

## Focused validation plan

All initial tests use synthetic users, keys, memberships, accounts and posts; no real credentials or provider network.

- Submitted content receives one ApprovalAction with actual issuer and trimmed reason; existing content/media/history remain unchanged and API audit identifies the operation.
- Draft refuses, then existing normal submit endpoint followed by approval succeeds.
- Stored key must explicitly include create_posts and approve_posts; issuer owner/admin alone is insufficient. Membership/custom-role demotion, revoked/expired key, deleted or changed issuer and allowlist removal after authentication refuse before mutation.
- Foreign author requires edit_others_posts; foreign workspace/account, cross-workspace child and a newly added out-of-scope child remain hidden with no partial decision.
- Every invalid or mixed child state, parent/child publish/schedule marker, retry marker, native checkpoint/intent and comment attempt marker rejects without clearing evidence.
- Repeated already-approved requests create no additional decision; actual concurrent PostgreSQL requests create exactly one decision.
- Two-stage approval stops at pending_client; repeated internal approval rejects and preserves the client stage.
- Injected service failure rolls back all target transitions and ApprovalAction. Request-audit failure does not erase the existing service's mandatory decision record.
- Post author/workspace, workflow mode, membership/custom role, key capability and account scope changes between initial lookup and locked admission are re-evaluated.
- No provider method, schedule transition, permission update, scope expansion or publishing task is called by this operation. Existing service-owned client reminders/notifications in two-stage mode are expected effects and are preserved and tested.

Initial focused tests and static checks require a source lease. Isolated PostgreSQL concurrency/full suite and normal public hosted CI require the agreed test/release window. A later reviewed Control Center source pin, normal deployment and real API workflow verification are distinct gates. No live write is authorized by this design document itself.

## Source validation receipt

The focused new approval tests plus existing submit, withdrawal and approved-schedule regression tests ran with temporary in-memory SQLite settings: 127 passed, 4 skipped in 10.48 seconds. All four skips are explicitly PostgreSQL-only lock tests; SQLite results do not establish concurrency safety. Those tests query pg_blocking_pids to establish an actual competing database lock wait before releasing the decision transaction. Native public PostgreSQL CI remains required.

Ruff check and formatting passed for the router and new tests. Narrow mypy passed for those two source files. git diff --check passed. The operation changes no permissions, workspace approval mode, API grant, provider state, schedule or runtime configuration. Existing two-stage service reminders and client notifications are expected workflow effects.
