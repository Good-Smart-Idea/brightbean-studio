"""Synthetic transport and durable audit contracts; never contact Meta."""

import copy
import json
from unittest.mock import patch

import httpx
import pytest

from apps.publisher.engine import PublishEngine
from apps.publisher.models import PublishLog
from providers.exceptions import APIError, PublishError, RateLimitError
from providers.instagram import InstagramProvider
from providers.meta_video import (
    _failure_diagnostic,
    _uri_diagnostic,
    native_failure_log_fields,
    publish_native_video,
)
from providers.types import PostType, PublishContent, PublishResult

from .test_native_video import native_case as native_case
from .test_submit_for_review import review_case as review_case

URI = "https://rupload.facebook.com/ig-api-upload/v25.0/123"
GENERIC = "Native video write outcome is unconfirmed; reconcile this session before retrying."


class Progress:
    def __init__(self, fail=None, exc=None):
        self.value = None
        self.phases = []
        self.fail = fail
        self.exc = exc

    def get(self, key):
        return self.value

    def set(self, key, value, timeout):
        phase = json.loads(value)["phase"]
        self.phases.append(phase)
        if phase == self.fail:
            raise self.exc
        self.value = value


@pytest.fixture
def upload(tmp_path):
    path = tmp_path / "asset.mp4"
    path.write_bytes(b"synthetic immutable video")
    content = PublishContent(
        text="synthetic",
        media_files=[str(path)],
        post_type=PostType.REEL,
        idempotency_key="synthetic-child",
        upload_cache=Progress(),
    )
    return content


def run(upload, handler):
    provider = InstagramProvider()
    client = httpx.Client
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return handler(request)

    def create():
        response = provider._request("POST", "https://graph.facebook.com/create").json()
        return response.get("id"), response.get("uri")

    def publish(container):
        response = provider._request("POST", "https://graph.facebook.com/publish").json()
        return PublishResult(platform_post_id=response.get("id", ""))

    with patch(
        "providers.base.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(transport), **kw)
    ):
        try:
            result = publish_native_video(
                provider,
                upload,
                target="1784",
                token="synthetic-grant",
                kind="ig-api-upload",
                create=create,
                wait=lambda _: None,
                publish=publish,
            )
            return result, calls
        except Exception as exc:
            return exc, calls


def success(request):
    if request.url.path == "/create":
        return httpx.Response(200, json={"id": "123", "uri": URI})
    if request.url.path == "/publish":
        return httpx.Response(200, json={"id": "456"})
    assert request.content == b"synthetic immutable video"
    return httpx.Response(200, json={"success": True})


@pytest.mark.parametrize(
    "path,stage,phases",
    [
        ("/create", "create_response", ["creating"]),
        ("/ig-api-upload/v25.0/123", "upload_http", ["creating", "created", "uploading"]),
        ("/publish", "publish_response", ["creating", "created", "uploading", "uploaded", "publishing"]),
    ],
)
def test_real_http_errors_retain_safe_codes_and_never_replay(upload, path, stage, phases):
    def reject(request):
        if request.url.path == path:
            return httpx.Response(
                403, json={"error": {"code": 190, "error_subcode": 463, "message": "PRIVATE provider body"}}
            )
        return success(request)

    error, calls = run(upload, reject)
    assert type(error) is PublishError and error.retryable is False
    assert str(error) == GENERIC
    fields = native_failure_log_fields(error)
    diagnostic = json.loads(fields["response_body"])
    assert fields["status_code"] == 403
    assert diagnostic["stage"] == stage
    assert diagnostic["cause_kind"] == "api" and diagnostic["cause_retryable"] is True
    assert diagnostic["provider_code"] == 190 and diagnostic["provider_subcode"] == 463
    assert upload.upload_cache.phases == phases
    assert "PRIVATE" not in json.dumps(diagnostic) and "https:" not in json.dumps(diagnostic)
    before = upload.upload_cache.value
    again, repeated = run(upload, lambda _: pytest.fail("must not replay ambiguous write"))
    assert type(again) is PublishError and again.retryable is False and not repeated
    assert upload.upload_cache.value == before
    assert calls[-1] == path


def test_success_request_and_checkpoint_order_unchanged(upload):
    result, calls = run(upload, success)
    assert result.platform_post_id == "456"
    assert calls == ["/create", "/ig-api-upload/v25.0/123", "/publish"]
    assert upload.upload_cache.phases == ["creating", "created", "uploading", "uploaded", "publishing", "complete"]
    again, calls = run(upload, lambda _: pytest.fail("confirmed checkpoint needs no HTTP"))
    assert again.platform_post_id == "456" and calls == []


@pytest.mark.parametrize(
    "failure,stage",
    [
        ("create_json", "create_response"),
        ("create_id", "create_ack"),
        ("upload_json", "upload_ack"),
        ("upload_false", "upload_ack"),
        ("publish_empty", "publish_ack"),
        ("timeout", "upload_http"),
    ],
)
def test_malformed_acknowledgements_and_transport_are_classified(upload, failure, stage):
    def handler(request):
        if failure == "create_json" and request.url.path == "/create":
            return httpx.Response(200, content=b"PRIVATE invalid JSON")
        if failure == "create_id" and request.url.path == "/create":
            return httpx.Response(200, json={"id": None, "uri": URI})
        if request.url.host == "rupload.facebook.com":
            if failure == "timeout":
                raise httpx.ReadTimeout("PRIVATE URL and grant", request=request)
            if failure == "upload_json":
                return httpx.Response(200, content=b"PRIVATE invalid JSON")
            if failure == "upload_false":
                return httpx.Response(200, json={"success": False})
        if failure == "publish_empty" and request.url.path == "/publish":
            return httpx.Response(200, json={})
        return success(request)

    error, _ = run(upload, handler)
    assert str(error) == GENERIC and error.retryable is False
    data = json.loads(native_failure_log_fields(error)["response_body"])
    assert data["stage"] == stage
    assert data["http_status"] == (200 if stage == "upload_ack" else None)
    assert "PRIVATE" not in json.dumps(data)
    if failure == "timeout":
        assert data["cause_kind"] == "transport"


@pytest.mark.parametrize("phase", ["creating", "created", "uploading", "uploaded", "publishing", "complete"])
def test_checkpoint_failure_preserves_original_policy_and_stops_next_write(upload, phase):
    original = RuntimeError("synthetic checkpoint failure")
    upload.upload_cache = Progress(fail=phase, exc=original)
    error, calls = run(upload, success)
    if phase == "created":  # Already inside the existing ambiguous-create catch.
        assert type(error) is PublishError and error.retryable is False and str(error) == GENERIC
    else:
        assert error is original and not hasattr(error, "retryable")
    data = json.loads(native_failure_log_fields(error)["response_body"])
    assert data["stage"] == "checkpoint_" + phase
    expected = {"creating": 0, "created": 1, "uploading": 1, "uploaded": 2, "publishing": 2, "complete": 3}
    assert len(calls) == expected[phase]


def test_unsafe_uri_classified_before_validation_without_credential_write(upload):
    uri = "http://PRIVATE:grant@rupload.facebook.com:8443/ig-api-upload/v14.2/123?PRIVATE=grant#PRIVATE"
    error, calls = run(upload, lambda _: httpx.Response(200, json={"id": "123", "uri": uri}))
    data = json.loads(native_failure_log_fields(error)["response_body"])
    assert data["stage"] == "upload_uri_validation" and calls == ["/create"]
    assert data["uri"] == {
        "present": True,
        "string": True,
        "https": False,
        "exact_host": True,
        "terminal_id_match": True,
        "query": True,
        "userinfo": True,
        "port": True,
        "fragment": True,
        "path_kind": "ig-api-upload",
        "api_version": [14, 2],
    }
    assert "PRIVATE" not in json.dumps(data) and "grant" not in json.dumps(data)
    assert json.loads(upload.upload_cache.value)["phase"] == "created"


@pytest.mark.parametrize(
    "uri",
    [
        None,
        123,
        "https://[",
        "https://foreign.example/video-upload/v9999.0/123",
        "https://rupload.facebook.com/video-upload/123",
    ],
)
def test_uri_classifier_is_closed_and_never_throws(uri):
    data = _failure_diagnostic("upload_uri_validation", uri=_uri_diagnostic(uri, "123"))
    fields = native_failure_log_fields(PublishError("synthetic", raw_response=data))
    assert fields and "foreign.example" not in fields["response_body"]


@pytest.mark.parametrize("value", [True, False, "190", -1, 2**31, 1.5])
def test_provider_integer_bounds_reject_bool_and_other_types(value):
    error = APIError("PRIVATE", status_code=value, raw_response={"error": {"code": value, "error_subcode": value}})
    data = _failure_diagnostic("create_response", error)
    assert data["http_status"] is None and data["provider_code"] is None and data["provider_subcode"] is None


def test_rate_limit_does_not_invent_http_status():
    data = _failure_diagnostic("create_response", RateLimitError("PRIVATE"))
    assert data["cause_kind"] == "rate_limit" and data["http_status"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("stage", "PRIVATE"),
        ("kind", "PRIVATE"),
        ("schema_version", True),
        ("provider_code", True),
        ("http_status", 999),
        ("extra", "PRIVATE"),
        ("uri", {"url": "PRIVATE"}),
        ("cause_kind", "PRIVATE"),
    ],
)
def test_audit_validator_rejects_untrusted_diagnostic(field, value):
    data = _failure_diagnostic("create_response")
    data[field] = value
    assert native_failure_log_fields(PublishError("synthetic", raw_response=data)) == {}


def test_hostile_exception_getters_and_string_cannot_mask_failure(upload):
    class HostileError(Exception):
        @property
        def retryable(self):
            raise ValueError("PRIVATE")

        def __str__(self):
            pytest.fail("diagnostics must never stringify the caught cause")

    error, _ = run(upload, lambda _: (_ for _ in ()).throw(HostileError()))
    assert type(error) is PublishError and str(error) == GENERIC
    assert native_failure_log_fields(error)
    assert native_failure_log_fields(HostileError()) == {}


@pytest.mark.django_db
def test_engine_persists_only_safe_diagnostic_and_preserves_history_terminal_state(native_case, upload):
    error, _ = run(upload, lambda _: httpx.Response(403, json={"error": {"code": 190, "message": "PRIVATE"}}))
    child = native_case[6]
    child.native_upload_state = {"progress": {"phase": "creating"}}
    child.save(update_fields=["native_upload_state"])
    prior = PublishLog.objects.create(
        platform_post=child, attempt_number=1, response_body='{"kind":"meta_publish_started"}'
    )
    saved = copy.deepcopy(child.native_upload_state)
    with patch.object(PublishEngine, "_dispatch_to_provider", side_effect=error):
        result = PublishEngine()._publish_platform_post(child)
    child.refresh_from_db()
    prior.refresh_from_db()
    log = child.publish_logs.exclude(pk=prior.pk).get()
    assert result == {"success": False, "error": GENERIC}
    assert child.status == "failed" and child.retry_count == 0 and child.next_retry_at is None
    assert child.native_upload_state == saved and prior.response_body == '{"kind":"meta_publish_started"}'
    assert log.error_message == GENERIC and log.status_code == 403
    assert json.loads(log.response_body)["provider_code"] == 190
    assert "PRIVATE" not in log.response_body


def test_json_failure_and_hostile_metadata_getter_fail_closed():
    error = PublishError("synthetic", raw_response=_failure_diagnostic("create_response"))
    with patch("providers.meta_video.json.dumps", side_effect=ValueError("PRIVATE")):
        assert native_failure_log_fields(error) == {}

    class GetterError(Exception):
        @property
        def raw_response(self):
            raise ValueError("PRIVATE")

    assert native_failure_log_fields(GetterError()) == {}


def test_safe_provider_code_does_not_change_credential_rejection():
    from apps.social_accounts.error_messages import is_credential_rejection

    cause = APIError("PRIVATE", status_code=403, raw_response={"error": {"code": 190}})
    error = PublishError(GENERIC, retryable=False, raw_response=_failure_diagnostic("create_response", cause))
    assert not is_credential_rejection(error)


@pytest.mark.parametrize("mutation", ["key_subclass", "kind_subclass", "uri_bool", "version_bool"])
def test_validator_rejects_string_subclasses_and_bad_nested_types(mutation):
    class UnsafeString(str):
        pass

    data = _failure_diagnostic("upload_uri_validation", uri=_uri_diagnostic(URI, "123"))
    if mutation == "key_subclass":
        data[UnsafeString("kind")] = data.pop("kind")
    elif mutation == "kind_subclass":
        data["kind"] = UnsafeString(data["kind"])
    elif mutation == "uri_bool":
        data["uri"]["https"] = 1
    else:
        data["uri"]["api_version"] = [True, 0]
    assert native_failure_log_fields(PublishError("synthetic", raw_response=data)) == {}
