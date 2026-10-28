"""Internal inspection of scoped original image bytes; no publishing integration.

The limits below are application inspection budgets, not Meta format limits.
A valid local image still has no approved/verified delivery route. The result
attests only to this read, not concurrent storage changes or provider eligibility.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage

from apps.media_library import storage
from apps.media_library.services import ImageTooLargeError, open_image
from apps.media_library.validators import sniff_mime

if TYPE_CHECKING:
    from apps.media_library.models import MediaAsset
    from apps.workspaces.models import Workspace

# Intentionally smaller than the default upload/decode budgets. A configured
# lower application cap wins. Neither limit is a provider-content contract.
MAX_INSPECTION_BYTES = 2 * 1024 * 1024
MAX_INSPECTION_PIXELS = 4_000_000
_CHUNK_SIZE = 64 * 1024
_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "CMYK", "I", "I;16", "F"})
Reason = Literal[
    "scope_denied",
    "not_completed_image",
    "missing_file",
    "unsupported_storage",
    "storage_unavailable",
    "size_limit",
    "pixel_limit",
    "unsupported_encoding",
    "animated_image",
    "mime_mismatch",
    "invalid_image",
    "inspection_policy_invalid",
]


@dataclass(frozen=True)
class StoredImageEvidence:
    local_status: Literal["valid", "blocked"]
    local_reason: Reason | None
    asset_id: UUID | None = None
    sha256: str | None = None
    byte_size: int | None = None
    format: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    mode: str | None = None
    exif_orientation: int | None = None
    delivery_status: Literal["blocked"] = "blocked"
    delivery_reason: Literal["no_approved_image_delivery_route"] = "no_approved_image_delivery_route"
    provider_contract: Literal["unknown"] = "unknown"
    provider_eligibility_verified: Literal[False] = False


def inspect_stored_image(asset: MediaAsset, *, workspace: Workspace) -> StoredImageEvidence:
    """Inspect ``asset.file`` only, using a trusted authorized workspace context.

    This internal helper grants no access. Callers must supply their authorized
    workspace and current model metadata; no API or publisher path calls it yet.
    Legacy assets without an organization fail closed. No FieldFile handle is
    opened/repositioned: local reads own a separate storage handle, and S3 reads
    use the existing ranged stream instead of django-storages' eager spool.
    Originals, versions, thumbnails, metadata and publication state are untouched.
    """
    if (
        workspace.pk is None
        or workspace.organization_id is None
        or asset.organization_id != workspace.organization_id
        or asset.workspace_id not in (workspace.pk, None)
    ):
        return StoredImageEvidence("blocked", "scope_denied")
    if asset.media_type != "image" or asset.processing_status != "completed":
        return StoredImageEvidence("blocked", "not_completed_image")
    if not asset.file or not asset.file.name:
        return StoredImageEvidence("blocked", "missing_file")

    def blocked(reason: Reason) -> StoredImageEvidence:
        return StoredImageEvidence("blocked", reason, asset_id=asset.pk)

    upload_cap = getattr(settings, "MEDIA_LIBRARY_MAX_IMAGE_SIZE", 20 * 1024 * 1024)
    pixel_cap = getattr(settings, "MEDIA_LIBRARY_MAX_IMAGE_PIXELS", 30_000_000)
    if any(
        type(value) is not int or value <= 0
        for value in (upload_cap, pixel_cap, MAX_INSPECTION_BYTES, MAX_INSPECTION_PIXELS)
    ):
        return blocked("inspection_policy_invalid")
    byte_limit = min(upload_cap, MAX_INSPECTION_BYTES)
    pixel_limit = min(pixel_cap, MAX_INSPECTION_PIXELS)
    if asset.file.storage is not default_storage:
        return blocked("unsupported_storage")
    try:
        if storage.is_s3_backend():
            # Inclusive end: bytes 0..limit request at most limit+1, so an
            # oversized object cannot look complete merely because of Range.
            stream = storage.open_object_range(asset.file.name, 0, byte_limit)
        elif isinstance(default_storage, FileSystemStorage):
            stream = default_storage.open(asset.file.name, "rb")
        else:
            return blocked("unsupported_storage")
        with contextlib.closing(stream), io.BytesIO() as body:
            digest = hashlib.sha256()
            count = 0
            while True:
                chunk = stream.read(min(_CHUNK_SIZE, byte_limit - count + 1))
                if type(chunk) is not bytes:
                    return blocked("storage_unavailable")
                if not chunk:
                    break
                count += len(chunk)
                if count > byte_limit:
                    return blocked("size_limit")
                digest.update(chunk)
                body.write(chunk)
            if count == 0:
                return blocked("invalid_image")
            actual_mime = sniff_mime(body)
            if actual_mime not in _FORMAT_MIME.values():
                return blocked("unsupported_encoding")
            if asset.mime_type != actual_mime:
                return blocked("mime_mismatch")
            try:
                with open_image(body) as image:
                    if image.width * image.height > pixel_limit:
                        return blocked("pixel_limit")
                    if _FORMAT_MIME.get(image.format) != actual_mime or image.mode not in _MODES:
                        return blocked("unsupported_encoding")
                    if getattr(image, "is_animated", False):
                        return blocked("animated_image")
                    image.verify()
                # verify() invalidates the decoder; reopen the same bounded
                # bytes and actually decode, without draft, conversion or edits.
                with open_image(body) as image:
                    if image.width * image.height > pixel_limit:
                        return blocked("pixel_limit")
                    image.load()
                    orientation = image.getexif().get(274)
                    return StoredImageEvidence(
                        "valid",
                        None,
                        asset_id=asset.pk,
                        sha256=digest.hexdigest(),
                        byte_size=count,
                        format=image.format,
                        mime_type=actual_mime,
                        width=image.width,
                        height=image.height,
                        mode=image.mode,
                        exif_orientation=orientation if type(orientation) is int and 1 <= orientation <= 8 else None,
                    )
            except ImageTooLargeError:
                return blocked("pixel_limit")
            except Exception:
                return blocked("invalid_image")
    except Exception:
        # Storage/backend messages can contain keys, URLs or credentials.
        return blocked("storage_unavailable")
