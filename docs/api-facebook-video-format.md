# Facebook draft video format

`GET` and `PATCH /api/v1/posts/{post_id}/platform-posts/{platform_post_id}/facebook-video-format` read or set one Facebook child's format. Both IDs are BrightBean UUIDs, not native Facebook receipt IDs.

PATCH accepts only:

```json
{"publish_as": "reel"}
```

The other supported value is `video` (regular Page video). Unknown values and additional fields are rejected. Generic provider metadata is never accepted or returned.

The response includes `post_id`, `platform_post_id`, `social_account_id`, `status`, `configured_publish_as` and `resolved_post_type`. The configured choice defaults to `video`. The resolved type uses the publisher's current attachment-based rules, so a stale Reel hint on an image resolves to `image` rather than claiming it will publish as a Reel.

## Scope and state

The bearer must be able to see the parent and every account target. GET is read-only. PATCH also requires `create_posts` and, for another author's post, `edit_others_posts`.

PATCH locks and rechecks the parent, children and media. The selected child must be an unsent Facebook draft with no native receipt, publication/schedule/retry markers. It requires exactly one completed, available video in the workspace or its shared organization library. A known Reel duration must be 3–90 seconds, matching the existing provider; zero means duration is unknown. Regular video has no Reel-duration limit.

Only the selected draft's format hint changes. Other metadata, post copy, review decisions, schedules and sibling children remain unchanged. A published YouTube sibling does not prevent changing a separate Facebook draft, provided every account is within the bearer scope.

Approved or pending-review children must use the existing explicit withdrawal/review workflow first. This endpoint does not withdraw approval, schedule, retry, publish, delete or convert a native post. An existing Facebook video may already be presented as a Reel by Meta; do not delete or repost it merely because this setting was absent.

## Errors

- `403`: missing edit permission.
- `404`: foreign workspace/account scope, wrong child or non-Facebook target.
- `409`: selected child is not an unsent draft.
- `422`: invalid format, extra fields, video shape/processing failure, or known Reel duration outside 3–90 seconds.

Success and failure requests use the normal API audit/rate-limit handling. Successful changes are audited as `post.facebook_video_format.update` against the selected child.
