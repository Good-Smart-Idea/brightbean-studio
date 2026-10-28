# Meta publication confirmation

An accepted API receipt is not native publication proof. Facebook video/Reel and
Facebook-login Instagram feed-media results require confirmation; Facebook
text/photos, Instagram Login and Stories retain their existing behavior. Other
providers keep their existing asynchronous contracts.

## Durable attempt boundary

Before invoking a covered provider, the engine commits an internal typed STARTED
PublishLog entry under a short row lock. It binds the child, canonical account,
workspace/author, actual copy/type, grant fingerprint, media ownership/storage
identity, and native-binary SHA-256/size manifest. No token, caption, temporary
file path, signed URL or file bytes are stored in that marker. STARTED has no
HTTP status because it records local intent, not a completed HTTP request.

Returned receipt, public/feed ID mapping, pending status and typed ACCEPTED audit
are committed atomically. Native Video IDs remain separate from Facebook feed
Post IDs. Cache recovery uses a distinct RECOVERED event with no invented HTTP
status. Success/retry saves update explicit fields and preserve checkpoints
written by the provider through a different ORM instance.

Confirmation requires that exact STARTED-to-ACCEPTED pair and unchanged inputs.
Ordinary logs cannot evict typed provenance from its bounded lookup. Old handles
without provenance remain unconfirmed; this change does not silently promote or
rewrite historical Published rows. A worker restart with an uncertain STARTED
attempt does not repeat a submit request. Native video may resume only its known
uploaded/complete checkpoint; ambiguous write phases stay blocked.

## Native evidence and links

All provider reads are GET-only. Facebook requires the exact Video node, scoped
Page owner, ready processing state, published=true and a validated permalink.
Instagram requires the exact published-media ID, canonical owner and valid
media type/permalink. If Instagram omits owner, one bounded canonical-account
media page can prove membership; absence is unknown. Metadata exceptions,
malformed fields and missing evidence use the unconfirmed budget. Definitive
native rejection fails without scheduling another publish.

Stored bytes are rehashed outside row locks before confirmation/replay, with a
streaming 1 GB bound. Native binary currently copies source bytes without
transcoding; the manifest therefore identifies the actual prepared upload.
After the GET, private bytes and immutable row/account/grant/input identity are
checked again before the final short status commit. A changed target, grant,
copy, media or hold prevents promotion. No network operation runs under that
row lock.

A successful confirmation GET appends only sanitized typed proof to PublishLog.
The analytics View Post button consumes that exact current child/native/account
proof and a validated HTTPS platform permalink. Facebook watch/photo essential
numeric query identifiers survive; tracking and credential parameters do not.
Legacy or unconfirmed links are disabled, not fabricated. This audit is retained
under the existing 90-day PublishLog policy; after proof expiry, no URL is guessed.

## Limits and operational acceptance

This is a confirmation/provenance fix, not a general media-delivery repair.
Hosted Instagram images/carousels and regular hosted Facebook video currently
have no actual transport-byte proof. They remain explicitly unconfirmed rather
than acquiring a fictional SHA or success claim. The protected media-URL/image
transport issue is separate; there is no public bucket or Cloudflare exception.

The existing live HVAC posts were manually published and do not prove this
BrightBean path. Acceptance requires an authorized unique test submitted through
the deployed fix, retained app receipt/provenance, and matching native platform
publication/permalink. The selected landscape test must preserve its approved
copy/media, previous humanizer coverage, and owner-selected accounts; no test
publication is performed by source validation.

Primary field contracts: Meta's official [Video SDK fields](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/advideo.py)
and [Instagram Media SDK fields](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/igmedia.py).
The confirmation request does not add a possibly unsupported Video post_id field;
public/feed mapping is retained only from the bound accepted API response.
