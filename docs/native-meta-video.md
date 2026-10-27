# Native Meta video uploads

A single Instagram Reel/video connected through Facebook Login, or an explicit
Facebook Page Reel, uploads the stored video bytes directly to Meta. It does not
ask Meta to fetch BrightBean's hosted asset URL. Instagram Login, images,
carousels, stories, and regular Facebook Page videos retain their existing paths.
This change does not alter Cloudflare Access or make media public. Native Instagram
Reels explicitly request `share_to_feed=true`; actual profile/feed placement still
requires native verification.

The engine reuses its storage-backed local media cache. Native dispatch never
refreshes credentials: Instagram uses the existing Facebook **user** grant on
that exact SocialAccount row; Facebook uses the connected Page grant. A missing
or known-expired Instagram user grant blocks before a provider write. Overrides
cannot redirect a native upload to another account.

## Configuration preflight

Authenticated `GET /api/v1/accounts/{account_id}/native-video-readiness` requires
`create_posts`, `publish_directly`, the workspace, and the API key's account
allowlist. Its only fields are `supported`, `configured`, `blocker`, and
`provider_eligibility_verified` (always false). It makes no provider request,
refresh, credential write, scheduling change, or publication. Configuration
presence does **not** establish current Meta business eligibility, permissions,
or successful publication. No credentials or upload URLs are returned.

## Durable progress and uncertain outcomes

`PlatformPost.native_upload_state` is an internal, non-editable JSON column,
excluded from generic API/composer input and `platform_extra`. Clones start empty.
Its key hashes the stable child identity, canonical target, caption, post type,
video size, and a streamed SHA-256 of the file bytes. Temporary paths and hosted
URL queries do not affect it; no caption, token, local path, or media URL is stored.

Each short transaction locks the child, rechecks workspace/account/content and
publishing state, and compares the previous checkpoint. Locks are released before
network I/O. The phases are creating, created, uploading, uploaded, publishing,
and complete. IDs are retained for reconciliation. Sessions expire after one hour;
expiry blocks reuse and never silently creates another session.

A processing timeout leaves `uploaded` intact. A new worker polls the same
container and then publishes it once. Before every external write its phase is
committed; a checkpoint failure stops the next write. Ambiguous create, binary
upload, or final publish outcomes are nonretryable and retain their phase. They
require reconciliation rather than blind replay. A completed checkpoint returns
the same media ID if a local outcome save was interrupted. PublishLog remains
HTTP outcome history and is not used as progress storage.

Upload destinations must be HTTPS on exactly `rupload.facebook.com`, with an
expected upload kind/version/ID path, and no credentials, port, query, or fragment.
The HTTP client does not follow redirects; non-2xx uploads cannot reach publish.
Files stream as raw bytes with OAuth, offset 0, file_size and Content-Length.
The existing 120-second Instagram processing poll limit is unchanged.

## Primary protocol references

- [Meta Instagram sample: Facebook user grant, resumable container, binary upload](https://github.com/fbsamples/reels_publishing_apis/blob/main/insta_reels_publishing_api_sample/index.js#L296-L335)
- [Meta Instagram sample instructions](https://github.com/fbsamples/reels_publishing_apis/blob/main/insta_reels_publishing_api_sample/README.md)
- [Meta Facebook sample: Page grant and binary body](https://github.com/fbsamples/reels_publishing_apis/blob/main/fb_reels_publishing_api_sample/index.js#L192-L205)
- [Meta official Facebook API collection](https://www.postman.com/meta/facebook/documentation/r56bjfd/facebook-api)

Synthetic tests verify the protocol, guards, resume, and checkpoint behavior.
Actual provider eligibility and native publication require separate authorized
verification after deployment; source/CI success does not establish either.
