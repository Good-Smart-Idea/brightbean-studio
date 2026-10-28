"""Synthetic stored-byte inspection only: no real storage, network or DB."""

import hashlib
import io
from dataclasses import FrozenInstanceError, asdict
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.files.storage import FileSystemStorage
from PIL import Image

from apps.publisher import image_delivery as inspection


class FakeLocalStorage(FileSystemStorage):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.calls = []
        self.streams = []

    def _open(self, name, mode="rb"):
        self.calls.append((name, mode))
        stream = io.BytesIO(self.data)
        self.streams.append(stream)
        return stream

    def _save(self, *args, **kwargs):
        pytest.fail("inspection must never save storage objects")

    def delete(self, *args, **kwargs):
        pytest.fail("inspection must never delete storage objects")

    def url(self, *args, **kwargs):
        pytest.fail("inspection must never derive a delivery URL")


def encoded(fmt="PNG", mode="RGBA", size=(4, 3), *, orientation=None):
    with Image.new(mode, size) as image, io.BytesIO() as body:
        kwargs = {}
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            kwargs["exif"] = exif
        image.save(body, format=fmt, **kwargs)
        return body.getvalue()


@pytest.fixture
def case(monkeypatch):
    workspace = SimpleNamespace(pk=uuid4(), organization_id=uuid4())
    data = encoded()
    storage = FakeLocalStorage(data)
    monkeypatch.setattr(inspection, "default_storage", storage)
    monkeypatch.setattr(inspection.storage, "is_s3_backend", lambda: False)
    asset = SimpleNamespace(
        pk=uuid4(),
        organization_id=workspace.organization_id,
        workspace_id=workspace.pk,
        media_type="image",
        processing_status="completed",
        mime_type="image/png",
        file_size=999999,
        width=999,
        height=999,
        file=SimpleNamespace(name="synthetic/private.png", storage=storage, handle=io.BytesIO(data)),
        thumbnail=SimpleNamespace(name="must-not-open.jpg"),
        current_version=SimpleNamespace(file="must-not-open"),
    )
    asset.file.handle.seek(5)
    return asset, workspace, storage


def inspect(case):
    return inspection.inspect_stored_image(case[0], workspace=case[1])


def assert_not_ready(result):
    assert result.delivery_status == "blocked"
    assert result.delivery_reason == "no_approved_image_delivery_route"
    assert result.provider_contract == "unknown"
    assert result.provider_eligibility_verified is False


@pytest.mark.parametrize(
    "fmt,mode,mime",
    [
        ("JPEG", "RGB", "image/jpeg"),
        ("JPEG", "CMYK", "image/jpeg"),
        ("PNG", "RGBA", "image/png"),
        ("PNG", "P", "image/png"),
        ("WEBP", "RGBA", "image/webp"),
    ],
)
def test_actual_encoding_dimensions_mode_digest_and_preservation(case, fmt, mode, mime):
    asset, _, storage = case
    storage.data = encoded(fmt, mode)
    asset.mime_type = mime
    original_fields = (asset.file_size, asset.width, asset.height, asset.file.name, asset.thumbnail.name)
    original_handle = asset.file.handle.getvalue()
    result = inspect(case)
    assert result.local_status == "valid" and result.local_reason is None
    assert (result.format, result.mime_type, result.width, result.height, result.mode) == (fmt, mime, 4, 3, mode)
    assert result.byte_size == len(storage.data) and result.sha256 == hashlib.sha256(storage.data).hexdigest()
    assert storage.calls == [(asset.file.name, "rb")]
    assert all(stream.closed for stream in storage.streams)
    assert (asset.file_size, asset.width, asset.height, asset.file.name, asset.thumbnail.name) == original_fields
    assert asset.file.handle.tell() == 5 and asset.file.handle.getvalue() == original_handle
    assert not asset.file.handle.closed
    assert_not_ready(result)
    with pytest.raises(FrozenInstanceError):
        result.width = 100


def test_exif_orientation_is_reported_without_transpose_or_conversion(case):
    asset, _, storage = case
    storage.data = encoded("JPEG", "RGB", (4, 3), orientation=6)
    asset.mime_type = "image/jpeg"
    result = inspect(case)
    assert result.local_status == "valid" and (result.width, result.height) == (4, 3)
    assert result.exif_orientation == 6
    assert result.sha256 == hashlib.sha256(storage.data).hexdigest()


@pytest.mark.parametrize(
    "scope",
    [
        "foreign_workspace",
        "foreign_org",
        "legacy_unknown_org",
        "shared_foreign_org",
        "shared_unknown_org",
        "workspace_unknown_org",
        "workspace_unsaved",
    ],
)
def test_scope_is_checked_before_any_storage_access(case, scope):
    asset, workspace, storage = case
    if scope == "foreign_workspace":
        asset.workspace_id = uuid4()
    elif scope == "foreign_org":
        asset.organization_id = uuid4()
    elif scope == "legacy_unknown_org":
        asset.organization_id = None
    elif scope.startswith("shared_"):
        asset.workspace_id = None
        asset.organization_id = uuid4() if scope == "shared_foreign_org" else None
    elif scope == "workspace_unknown_org":
        workspace.organization_id = None
    else:
        workspace.pk = None
    with patch.object(inspection.storage, "is_s3_backend", side_effect=AssertionError("no storage access")):
        result = inspect(case)
    assert result.local_status == "blocked" and result.local_reason == "scope_denied"
    assert result.asset_id is None and result.sha256 is None and storage.calls == []
    assert_not_ready(result)


def test_same_organization_shared_asset_is_allowed_locally(case):
    case[0].workspace_id = None
    result = inspect(case)
    assert result.local_status == "valid"
    assert_not_ready(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("media_type", "gif"),
        ("media_type", "video"),
        ("processing_status", "pending"),
        ("processing_status", "processing"),
        ("processing_status", "failed"),
    ],
)
def test_only_completed_images_are_opened(case, field, value):
    setattr(case[0], field, value)
    assert inspect(case).local_reason == "not_completed_image"
    assert case[2].calls == []


def test_missing_file_stops_before_storage(case):
    case[0].file = None
    assert inspect(case).local_reason == "missing_file"
    assert case[2].calls == []


@pytest.mark.parametrize(
    "data,mime,reason",
    [
        (b"<html>PRIVATE</html>", "image/jpeg", "unsupported_encoding"),
        (b"\x89PNG\r\n\x1a\nPRIVATE", "image/png", "invalid_image"),
        (b"", "image/png", "invalid_image"),
    ],
)
def test_html_corrupt_and_empty_bytes_return_closed_reasons(case, data, mime, reason):
    case[0].mime_type, case[2].data = mime, data
    result = inspect(case)
    assert result.local_reason == reason and result.sha256 is None
    assert "PRIVATE" not in repr(asdict(result))
    assert all(stream.closed for stream in case[2].streams)


@pytest.mark.parametrize("mime", ["image/jpeg", "text/html", ""])
def test_stored_mime_must_match_actual_magic(case, mime):
    case[0].mime_type = mime
    assert inspect(case).local_reason == "mime_mismatch"


def test_decoder_format_must_match_sniffed_magic(case):
    with patch.object(inspection, "sniff_mime", return_value="image/jpeg"):
        case[0].mime_type = "image/jpeg"
        assert inspect(case).local_reason == "unsupported_encoding"


def test_truncated_jpeg_rejected_by_full_decode(case):
    case[0].mime_type = "image/jpeg"
    case[2].data = encoded("JPEG", "RGB")[:-8]
    assert inspect(case).local_reason == "invalid_image"


@pytest.mark.parametrize("budget", ["application", "configured"])
def test_byte_budget_bounds_actual_read_not_asset_size_metadata(case, monkeypatch, settings, budget):
    limit = len(case[2].data) - 1
    case[0].file_size = 1
    if budget == "application":
        monkeypatch.setattr(inspection, "MAX_INSPECTION_BYTES", limit)
    else:
        settings.MEDIA_LIBRARY_MAX_IMAGE_SIZE = limit
    result = inspect(case)
    assert result.local_reason == "size_limit" and result.sha256 is None
    assert all(stream.closed for stream in case[2].streams)


@pytest.mark.parametrize("budget", ["application", "configured"])
def test_pixel_budget_rejects_before_decode_with_tiny_fixture(case, monkeypatch, settings, budget):
    if budget == "application":
        monkeypatch.setattr(inspection, "MAX_INSPECTION_PIXELS", 11)
    else:
        settings.MEDIA_LIBRARY_MAX_IMAGE_PIXELS = 11
    with patch.object(Image.Image, "load", side_effect=AssertionError("must guard before decode")):
        result = inspect(case)
    assert result.local_reason == "pixel_limit" and result.sha256 is None


def test_pillow_decompression_backstop_remains_active(case, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    with patch.object(Image.Image, "load", side_effect=AssertionError("no decode")):
        assert inspect(case).local_reason == "pixel_limit"


@pytest.mark.parametrize("value", [True, 0, -1, "100"])
def test_invalid_inspection_policy_fails_before_open(case, monkeypatch, value):
    monkeypatch.setattr(inspection, "MAX_INSPECTION_BYTES", value)
    assert inspect(case).local_reason == "inspection_policy_invalid"
    assert case[2].calls == []


def test_animated_png_is_not_treated_as_one_still_image(case):
    with Image.new("RGBA", (4, 3)) as first, Image.new("RGBA", (4, 3), "red") as second, io.BytesIO() as data:
        first.save(data, format="PNG", save_all=True, append_images=[second], duration=100)
        case[2].data = data.getvalue()
    assert inspect(case).local_reason == "animated_image"


def test_gif_mislabeled_as_image_is_not_a_supported_still_encoding(case):
    case[0].mime_type = "image/gif"
    case[2].data = encoded("GIF", "P")
    assert inspect(case).local_reason == "unsupported_encoding"


def test_s3_uses_inclusive_range_cap_plus_one_and_closes_stream(case, monkeypatch):
    asset, _, local = case
    remote = object()
    asset.file.storage = remote
    monkeypatch.setattr(inspection, "default_storage", remote)
    monkeypatch.setattr(inspection.storage, "is_s3_backend", lambda: True)
    stream = io.BytesIO(local.data)
    with patch.object(inspection.storage, "open_object_range", return_value=stream) as opener:
        result = inspect(case)
    assert result.local_status == "valid" and stream.closed
    opener.assert_called_once_with(asset.file.name, 0, inspection.MAX_INSPECTION_BYTES)
    assert local.calls == []


def test_s3_oversize_range_or_ignored_range_is_still_bounded(case, monkeypatch):
    asset, _, _ = case
    remote = object()
    asset.file.storage = remote
    monkeypatch.setattr(inspection, "default_storage", remote)
    monkeypatch.setattr(inspection.storage, "is_s3_backend", lambda: True)
    monkeypatch.setattr(inspection, "MAX_INSPECTION_BYTES", 10)

    class TrackedStream(io.BytesIO):
        def __init__(self):
            super().__init__(b"x" * 100)
            self.read_sizes = []
            self.consumed = 0

        def read(self, n=-1):
            self.read_sizes.append(n)
            data = super().read(n)
            self.consumed += len(data)
            return data

    stream = TrackedStream()
    with patch.object(inspection.storage, "open_object_range", return_value=stream) as opener:
        result = inspect(case)
    assert result.local_reason == "size_limit" and result.sha256 is None
    opener.assert_called_once_with(asset.file.name, 0, 10)  # Inclusive 0..10 = eleven bytes.
    assert stream.consumed == 11 and stream.read_sizes == [11] and stream.closed


def test_unknown_backend_or_nondefault_asset_storage_is_not_opened(case, monkeypatch):
    unknown = SimpleNamespace(open=lambda *_: pytest.fail("unknown backend must not open"))
    monkeypatch.setattr(inspection, "default_storage", unknown)
    case[0].file.storage = unknown
    assert inspect(case).local_reason == "unsupported_storage"
    case[0].file.storage = object()
    assert inspect(case).local_reason == "unsupported_storage"


@pytest.mark.parametrize("failure", ["open", "read", "close", "nonbytes"])
def test_storage_failures_never_leak_messages_or_partial_digest(case, failure):
    class BrokenStream(io.BytesIO):
        def read(self, n=-1):
            if failure == "read":
                raise OSError("PRIVATE signed URL credential")
            if failure == "nonbytes":
                return "PRIVATE"
            return super().read(n)

        def close(self):
            super().close()
            if failure == "close":
                raise OSError("PRIVATE signed URL credential")

    stream = BrokenStream(case[2].data)
    with patch.object(
        case[2], "open", side_effect=OSError("PRIVATE") if failure == "open" else None, return_value=stream
    ):
        result = inspect(case)
    assert result.local_reason == "storage_unavailable" and result.sha256 is None
    assert "PRIVATE" not in repr(asdict(result)) and "synthetic/private" not in repr(asdict(result))
    if failure != "open":
        assert stream.closed
    else:
        stream.close()
    assert_not_ready(result)
