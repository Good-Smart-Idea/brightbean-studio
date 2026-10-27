# Schedule existing approved posts

`POST /api/v1/posts/{post_id}/schedule` and MCP `schedule_draft` accept existing drafts or final-approved platform children. Approval alone does not enqueue publication; scheduling is a separate action requiring `create_posts` and `publish_directly`, plus `edit_others_posts` for another author's post.

Drafts still obey the workspace approval requirement. `pending_review` and `pending_client` cannot be scheduled. Final-approved children can be scheduled in either required approval mode, after platform media and quota validation.

Every child must be eligible and within the key's workspace/account scope. Mixed noneligible states reject the entire operation. Repeating the same schedule for all already-scheduled children succeeds without resetting publish attempts or handles; changing that time requires the existing update flow. Scheduling does not create duplicate posts.
