"""Each Google Business location connects and publishes as its own account."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.urls import reverse

from apps.credentials.models import PlatformCredential
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.publisher.engine import _resolve_publish_credentials
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.views import OAUTH_SESSION_KEY, _sign_state
from apps.workspaces.models import Workspace
from providers.google_business import GoogleBusinessProvider
from providers.types import OAuthTokens, PublishContent

ORG_PIN = "accounts/9/locations/111"


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Test WS", organization=organization)


@pytest.fixture
def authenticated_client(client, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    client.force_login(user)
    return client


def _oauth_get(client, workspace, user, platform="google_business"):
    nonce = "nonce-gbp"
    state = _sign_state(workspace.id, platform, user.id, nonce)
    session = client.session
    session[OAUTH_SESSION_KEY] = {"nonce": nonce}
    session.save()
    return reverse("social_accounts:oauth_callback", kwargs={"platform": platform}), {"code": "abc", "state": state}


def _org_credential(organization, **extra):
    PlatformCredential.objects.create(
        organization=organization,
        platform="google_business",
        credentials={"client_id": "cid", "client_secret": "sec", **extra},
    )


def _account(workspace, platform_id, name, token):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="google_business",
        account_platform_id=platform_id,
        account_name=name,
        oauth_access_token=token,
    )


def _record(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/localPosts"):
            parent = path.removeprefix("/v4/").removesuffix("/localPosts")
            return httpx.Response(
                200, json={"name": f"{parent}/localPosts/made", "searchUrl": "https://example.test/p"}
            )
        if request.method == "GET" and "/localPosts/" in path:
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    original = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return seen


@pytest.mark.django_db
class TestChooseGoogleLocation:
    def test_several_locations_go_to_the_picker(self, authenticated_client, workspace, user):
        provider = MagicMock()
        provider.exchange_code.return_value = OAuthTokens(
            access_token="user-token", refresh_token="refresh", expires_in=3600
        )
        provider.list_connectable_locations.return_value = [
            {"id": "accounts/9/locations/1", "name": "Cafe", "category": "1 Main"},
            {"id": "accounts/9/locations/2", "name": "Kiosk", "category": "2 Main"},
        ]
        url, query = _oauth_get(authenticated_client, workspace, user)

        with patch("apps.social_accounts.views._get_provider_for_platform", return_value=provider):
            response = authenticated_client.get(url, query)

        assert response.status_code == 302
        assert response.url == reverse("social_accounts:select_account")
        provider.get_profile.assert_not_called()
        pages = authenticated_client.session["oauth_page_select"]["pages"]
        assert [page["id"] for page in pages] == ["accounts/9/locations/1", "accounts/9/locations/2"]
        assert authenticated_client.session["oauth_page_select"]["user_tokens"]["access_token"] == "user-token"

    def test_choosing_one_location_stores_its_resource_names(self, authenticated_client, workspace):
        session = authenticated_client.session
        session["oauth_page_select"] = {
            "workspace_id": str(workspace.id),
            "platform": "google_business",
            "user_tokens": {"access_token": "user-token", "refresh_token": "refresh", "expires_in": 3600},
            "pages": [
                {"id": "accounts/9/locations/1", "name": "Cafe"},
                {"id": "accounts/9/locations/2", "name": "Kiosk"},
            ],
        }
        session.save()

        response = authenticated_client.post(
            reverse("social_accounts:select_account"),
            {"selected_pages": ["accounts/9/locations/2"]},
        )

        assert response.status_code == 302
        account = SocialAccount.objects.get(workspace=workspace, platform="google_business")
        assert account.account_platform_id == "accounts/9/locations/2"
        assert account.account_name == "Kiosk"
        assert account.oauth_access_token == "user-token"
        assert account.oauth_refresh_token == "refresh"

    def test_reconnecting_a_legacy_location_updates_that_row(self, authenticated_client, workspace):
        legacy = SocialAccount.objects.create(
            workspace=workspace,
            platform="google_business",
            account_platform_id="locations/2",
            account_name="Old Kiosk",
            oauth_access_token="old-token",
        )
        session = authenticated_client.session
        session["oauth_page_select"] = {
            "workspace_id": str(workspace.id),
            "platform": "google_business",
            "user_tokens": {"access_token": "user-token", "refresh_token": "refresh", "expires_in": 3600},
            "pages": [{"id": "accounts/9/locations/2", "name": "Kiosk"}],
        }
        session.save()

        authenticated_client.post(
            reverse("social_accounts:select_account"),
            {"selected_pages": ["accounts/9/locations/2"]},
        )

        legacy.refresh_from_db()
        assert SocialAccount.objects.filter(workspace=workspace, platform="google_business").count() == 1
        assert legacy.account_platform_id == "accounts/9/locations/2"
        assert legacy.account_name == "Kiosk"
        assert legacy.oauth_access_token == "user-token"

    def test_a_single_location_connects_without_the_picker(self, authenticated_client, workspace, user):
        provider = MagicMock()
        provider.exchange_code.return_value = OAuthTokens(
            access_token="user-token", refresh_token="refresh", expires_in=3600
        )
        provider.list_connectable_locations.return_value = [
            {"id": "accounts/9/locations/1", "name": "Cafe", "category": "1 Main"},
        ]
        url, query = _oauth_get(authenticated_client, workspace, user)

        with patch("apps.social_accounts.views._get_provider_for_platform", return_value=provider):
            response = authenticated_client.get(url, query)

        assert response.status_code == 302
        assert response.url == reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
        provider.get_profile.assert_not_called()
        account = SocialAccount.objects.get(workspace=workspace, platform="google_business")
        assert account.account_platform_id == "accounts/9/locations/1"
        assert account.account_name == "Cafe"
        assert "oauth_page_select" not in authenticated_client.session


@pytest.mark.django_db
class TestGoogleBusinessPublishesToItsOwnLocation:
    def test_two_accounts_in_one_org_publish_and_report_on_their_own_locations(
        self, organization, workspace, monkeypatch, settings
    ):
        """An org pin must not send every connection to the same location."""
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {"google_business": {"client_id": "", "client_secret": ""}}
        _org_credential(organization, account_id="accounts/9", location_id=ORG_PIN)
        cafe = _account(workspace, "accounts/9/locations/1", "Cafe", "tok-cafe")
        kiosk = _account(workspace, "accounts/9/locations/2", "Kiosk", "tok-kiosk")
        seen = _record(monkeypatch)

        for account, location in ((cafe, "1"), (kiosk, "2")):
            provider = GoogleBusinessProvider(_resolve_publish_credentials(account))
            result = provider.publish_post(account.oauth_access_token, PublishContent(text=f"hello {location}"))
            metrics = provider.get_post_metrics(account.oauth_access_token, "bare-post")

            parent = f"accounts/9/locations/{location}"
            assert result.platform_post_id == f"{parent}/localPosts/made"
            assert metrics.impressions == 0

        posts = [request.url.path for request in seen if request.method == "POST"]
        reads = [request.url.path for request in seen if request.method == "GET"]
        assert posts == [
            "/v4/accounts/9/locations/1/localPosts",
            "/v4/accounts/9/locations/2/localPosts",
        ]
        assert reads == [
            "/v4/accounts/9/locations/1/localPosts/bare-post",
            "/v4/accounts/9/locations/2/localPosts/bare-post",
        ]
        assert all(ORG_PIN not in request.url.path for request in seen)

    def test_a_legacy_location_id_is_used_instead_of_the_org_pin(self, organization, workspace, monkeypatch, settings):
        """Accounts connected before the picker stored ``locations/{id}`` only."""
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {"google_business": {"client_id": "", "client_secret": ""}}
        _org_credential(organization, location_id=ORG_PIN)
        legacy = _account(workspace, "locations/456", "Cafe", "tok-legacy")
        seen = _record(monkeypatch)

        provider = GoogleBusinessProvider(_resolve_publish_credentials(legacy))
        result = provider.publish_post(legacy.oauth_access_token, PublishContent(text="still here"))
        provider.get_post_metrics(legacy.oauth_access_token, "bare-post")

        assert result.platform_post_id == "accounts/9/locations/456/localPosts/made"
        assert [request.url.path for request in seen] == [
            "/v4/accounts/9/locations/456/localPosts",
            "/v4/accounts/9/locations/456/localPosts/bare-post",
        ]
