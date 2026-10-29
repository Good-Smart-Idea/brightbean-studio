"""Raw TikTok creator interaction contracts, without database or network access."""

import json
from types import SimpleNamespace

import pytest
from django.test import RequestFactory

import providers
from apps.composer import views
from apps.credentials import models as credential_models

FLAGS = ("comment_disabled", "duet_disabled", "stitch_disabled")
MISSING = object()


def creator_info(values=(False, False, False)):
    return {
        "creator_nickname": "Test Creator",
        "privacy_level_options": ["SELF_ONLY"],
        "max_video_post_duration_sec": 120,
        **dict(zip(FLAGS, values, strict=True)),
    }


@pytest.fixture
def query_creator(monkeypatch):
    workspace = SimpleNamespace(id="workspace-id", organization_id="organization-id")

    def no_refresh(*args, **kwargs):
        raise AssertionError("Creator contract tests must never refresh tokens")

    account = SimpleNamespace(
        oauth_access_token="fake-access-token",
        token_expires_at=None,
        refresh_oauth_token=no_refresh,
    )
    credentials = object()
    calls = []
    raw_info = None

    def get_workspace(request, workspace_id):
        assert request.user.is_authenticated
        assert workspace_id == workspace.id
        return workspace

    def get_account(model, **kwargs):
        assert model is views.SocialAccount
        assert kwargs == {"id": "account-id", "workspace": workspace, "platform": "tiktok"}
        return account

    def resolve_credentials(platform, organization_id):
        assert (platform, organization_id) == ("tiktok", workspace.organization_id)
        return credentials

    def query(access_token):
        assert access_token == account.oauth_access_token
        calls.append(access_token)
        return raw_info

    provider = SimpleNamespace(query_creator_info=query)

    def get_provider(platform, resolved):
        assert platform == "tiktok"
        assert resolved is credentials
        return provider

    monkeypatch.setattr(views, "_get_workspace", get_workspace)
    monkeypatch.setattr(views, "get_object_or_404", get_account)
    monkeypatch.setattr(credential_models, "resolve_platform_credentials", resolve_credentials)
    monkeypatch.setattr(providers, "get_provider", get_provider)

    def invoke(info):
        nonlocal raw_info
        raw_info = info
        request = RequestFactory().get("/creator-info/")
        request.user = SimpleNamespace(is_authenticated=True)
        response = views.tiktok_creator_info(request, workspace.id, "account-id")
        assert response.status_code == 200
        assert calls == [account.oauth_access_token]
        return json.loads(response.content)

    return invoke


def assert_unavailable(payload):
    assert payload == {
        "available": False,
        "error": "Invalid creator interaction settings",
        "creator_nickname": "",
        "privacy_level_options": [],
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": None,
    }


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize(
    "invalid",
    [MISSING, None, "false", "", 0, 1],
    ids=["missing", "null", "string-false", "empty-string", "zero", "one"],
)
def test_rejects_each_missing_or_non_boolean_flag(query_creator, flag, invalid):
    info = creator_info()
    if invalid is MISSING:
        del info[flag]
    else:
        info[flag] = invalid
    assert_unavailable(query_creator(info))


@pytest.mark.parametrize("info", [None, [], "false", 0, 1, False, True])
def test_rejects_non_dict_creator_info(query_creator, info):
    assert_unavailable(query_creator(info))


@pytest.mark.parametrize(
    "values",
    [(False, False, False), (True, True, True), (False, True, False), (True, False, True)],
    ids=["all-false", "all-true", "mixed-duet-disabled", "mixed-duet-enabled"],
)
def test_preserves_valid_raw_booleans(query_creator, values):
    info = creator_info(values)
    payload = query_creator(info)
    assert payload == {"available": True, **info}
    for flag, value in zip(FLAGS, values, strict=True):
        assert type(payload[flag]) is bool
        assert payload[flag] is value
