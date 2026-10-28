"""Malformed durable provenance must never authorize confirmation or a resend."""

import json
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from apps.publisher import meta_receipts
from apps.publisher.models import PublishLog

from .test_meta_confirmation import receipt_case as receipt_case
from .test_meta_confirmation import sweep


def records(case):
    return [(log, json.loads(log.response_body)) for log in case.child.publish_logs.order_by("created_at")]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_id", None),
        ("attempt_id", ""),
        ("attempt_id", "not-a-uuid"),
        ("attempt_id", "00000000000000000000000000000000"),
        ("binding", None),
        ("binding", ""),
        ("binding", "z" * 64),
        ("binding", "A" * 64),
        ("binding", "a" * 63),
        ("grant_kind", "page_fallback"),
        ("content_type", "unrecognized"),
        ("media_bytes_verified", 1),
        ("media_bytes_verified", "true"),
        ("manifest", None),
        ("manifest", {}),
        ("child_id", None),
        ("account_id", None),
        ("workspace_id", None),
        ("owner_id", True),
        ("owner_id", "١١١"),
        ("unexpected_key", "ignored-field-must-fail"),
    ],
)
def test_matching_malformed_pairs_do_not_become_trusted(receipt_case, field, value):
    case = receipt_case
    for log, marker in records(case):
        marker[field] = value
        log.response_body = json.dumps(marker, separators=(",", ":"))
        log.save(update_fields=["response_body"])
    assert meta_receipts.accepted_attempt(case.child) is None
    case.provider._request = MagicMock(side_effect=AssertionError("malformed provenance cannot authorize GET"))
    assert sweep(case) == 0
    case.provider._request.assert_not_called()
    assert case.child.status == "publishing"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field", ["attempt_id", "binding", "grant_kind", "content_type", "manifest", "media_bytes_verified"]
)
def test_missing_common_fields_cannot_pair_through_none_equality(receipt_case, field):
    case = receipt_case
    for log, marker in records(case):
        marker.pop(field)
        log.response_body = json.dumps(marker, separators=(",", ":"))
        log.save(update_fields=["response_body"])
    assert meta_receipts.accepted_attempt(case.child) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("receipt_id", None),
        ("receipt_id", ""),
        ("receipt_id", "٥٥٥"),
        ("receipt_id", True),
        ("receipt_id", "5" * 101),
        ("public_post_id", None),
        ("public_post_id", "bad"),
    ],
)
def test_invalid_accepted_handles_are_untrusted(receipt_case, field, value):
    case = receipt_case
    log, marker = records(case)[-1]
    marker[field] = value
    log.response_body = json.dumps(marker, separators=(",", ":"))
    log.save(update_fields=["response_body"])
    assert meta_receipts.accepted_attempt(case.child) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("asset_id", "not-a-uuid"),
        ("sha256", "z" * 64),
        ("sha256", "A" * 64),
        ("size", True),
        ("size", -1),
        ("size", "20"),
        ("unexpected_key", 1),
    ],
)
def test_malformed_manifest_cannot_pair_even_when_identical(receipt_case, field, value):
    case = receipt_case
    for log, marker in records(case):
        marker["manifest"][0][field] = value
        log.response_body = json.dumps(marker, separators=(",", ":"))
        log.save(update_fields=["response_body"])
    assert meta_receipts.accepted_attempt(case.child) is None


@pytest.mark.django_db
@pytest.mark.parametrize("field,value", [("content_type", "video"), ("media_bytes_verified", False), ("manifest", [])])
def test_conflicting_common_fields_are_not_a_valid_pair(receipt_case, field, value):
    case = receipt_case
    log, marker = records(case)[-1]
    marker[field] = value
    log.response_body = json.dumps(marker, separators=(",", ":"))
    log.save(update_fields=["response_body"])
    assert meta_receipts.accepted_attempt(case.child) is None


@pytest.mark.django_db
def test_valid_pair_survives_many_unrelated_logs(receipt_case):
    case = receipt_case
    expected = deepcopy(meta_receipts.accepted_attempt(case.child))
    assert expected is not None
    PublishLog.objects.bulk_create([PublishLog(platform_post=case.child, response_body="unrelated") for _ in range(40)])
    assert meta_receipts.accepted_attempt(case.child) == expected
