# Return a review submission to draft

`POST /api/v1/posts/{post_id}/return-to-draft` withdraws a pending internal or client review. It never schedules, approves, publishes, or deletes the post.

The bearer needs `create_posts`, and `edit_others_posts` if another user authored the post. The post must belong to the key's workspace, and every target account must be in the key's allowlist. Out-of-scope posts return 404.

All platform children must be `draft`, `pending_review`, or `pending_client`; any other state rejects the whole request with 409. Eligible children move to draft atomically. Repeating the request on drafts succeeds without duplicating the approval audit action.

The response is the normal PostResponse. The approval history records Review Withdrawn, and reminders are removed only when no sibling remains in that review stage. Existing caption, media, proposal date and unrelated post states are retained. A subsequent submit-for-review call can request fresh approval when wanted.
