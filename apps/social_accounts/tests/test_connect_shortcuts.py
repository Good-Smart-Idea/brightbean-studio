"""Sidebar platform shortcuts navigate to their platform card on the Connect page.

CTRL-124: each sidebar Connect-channels shortcut must link to a stable URL
fragment matching the unique HTML ID of that platform's card, "More channels"
stays generic, and an unconfigured card must never expose a connect form.
"""

import re
from html.parser import HTMLParser

import pytest
from django.urls import reverse

CARD_ID_RE = re.compile(r'id="connect-([a-z_]+)"')


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Test WS", organization=organization)


@pytest.fixture
def manager_setup(db, user, organization, workspace):
    from apps.members.models import OrgMembership, WorkspaceMembership

    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    return user


@pytest.fixture
def authenticated_client(client, user, manager_setup):
    client.force_login(user)
    return client


@pytest.fixture
def connect_url(workspace):
    return reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})


class _SidebarAnchorParser(HTMLParser):
    """Collect each <a> anchor's own href and label, scoped to sidebar nav items."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str]] = []
        self._depth = 0
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        if self._depth == 0:
            classes = (dict(attrs).get("class") or "").split()
            if "sidebar-nav-item" in classes:
                self._depth = 1
                self._href = dict(attrs).get("href") or ""
                self._text = []
        else:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag != "a" or not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            self.anchors.append({"href": self._href, "text": "".join(self._text).strip()})

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)


def _sidebar_anchors(content: str) -> list[dict[str, str]]:
    parser = _SidebarAnchorParser()
    parser.feed(content)
    return parser.anchors


def test_sidebar_shortcut_fragment_matches_platform_card(authenticated_client, connect_url):
    """Each sidebar shortcut links to exactly connect_url#connect-<card id>."""
    response = authenticated_client.get(connect_url)
    assert response.status_code == 200

    content = response.content.decode()
    shortcuts = [a for a in _sidebar_anchors(content) if a["href"].startswith(connect_url + "#connect-")]
    assert shortcuts, "sidebar should render at least one platform shortcut"

    card_ids = CARD_ID_RE.findall(content)
    assert len(card_ids) == len(set(card_ids)), "platform card IDs must be unique"
    card_id_set = set(card_ids)

    for shortcut in shortcuts:
        href = shortcut["href"]
        fragment = href[len(connect_url) + len("#connect-") :]
        assert fragment in card_id_set, f"sidebar shortcut {href} has no matching platform card"


def test_more_channels_link_is_generic(authenticated_client, connect_url):
    """The 'More channels' shortcut goes to the chooser with no platform fragment."""
    response = authenticated_client.get(connect_url)
    assert response.status_code == 200
    content = response.content.decode()
    more = [a for a in _sidebar_anchors(content) if a["text"] == "More channels"]
    assert more, "'More channels' sidebar link should render"
    for anchor in more:
        assert anchor["href"] == connect_url, f"'More channels' must link to {connect_url} with no platform fragment"


def test_unconfigured_platform_card_cannot_initiate_oauth(authenticated_client, workspace, connect_url):
    """A platform without credentials renders 'Not Configured' with no connect form."""
    from apps.credentials.models import PlatformCredential

    PlatformCredential.objects.create(
        organization=workspace.organization,
        platform="threads",
        credentials={},
        is_configured=False,
    )
    response = authenticated_client.get(connect_url)
    assert response.status_code == 200

    content = response.content.decode()
    card = re.search(r'<div id="connect-threads".*?(?=<div id="connect-|\Z)', content, re.S)
    assert card, "threads card should render with its platform ID"
    assert "Not Configured" in card.group(0)
    assert 'method="post"' not in card.group(0)
