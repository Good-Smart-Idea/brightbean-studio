"""Regression coverage for playable, uncropped editorial video previews."""

from html.parser import HTMLParser
from types import SimpleNamespace
from uuid import uuid4

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class VideoMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.videos = []
        self.links = []
        self.blocking_overlays = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "video":
            self.videos.append(attrs)
        if tag == "a":
            self.links.append(attrs)
        classes = attrs.get("class", "")
        if "absolute inset-0 flex items-center justify-center" in classes and "pointer-events-none" not in classes:
            self.blocking_overlays.append(attrs)


class VideoPlaybackTemplateTests(SimpleTestCase):
    def preview(self, platform, count=1):
        account = SimpleNamespace(
            platform=platform, account_name="Preview Account", account_handle="preview", avatar_url=""
        )
        context = {
            "previews": [
                {
                    "account": account,
                    "caption": "A caption",
                    "truncated_caption": "A caption",
                    "char_count": 9,
                    "char_limit": 3000,
                }
            ],
            "media_items": [
                {"url": f"/media/clip-{i}.mp4", "is_video": True, "filename": f"clip-{i}.mp4"} for i in range(count)
            ],
        }
        return render_to_string("composer/partials/preview_panel.html", context)

    def test_all_platforms_render_native_controls_without_cropping(self):
        for platform in [
            "youtube",
            "instagram",
            "instagram_login",
            "facebook",
            "linkedin_personal",
            "linkedin_company",
            "tiktok",
            "threads",
            "bluesky",
            "pinterest",
            "mastodon",
        ]:
            for count in [1, 2]:
                with self.subTest(platform=platform, count=count):
                    markup = VideoMarkup(self.preview(platform, count))
                    self.assertTrue(markup.videos)
                    self.assertFalse(markup.blocking_overlays)
                    for video in markup.videos:
                        self.assertIn("controls", video)
                        self.assertIn("playsinline", video)
                        self.assertIn("object-contain", video["class"])
                        self.assertNotIn("object-cover", video["class"])
                        self.assertNotIn("#t=", video["src"])
                        self.assertTrue(video["aria-label"])

    def test_single_feed_videos_keep_intrinsic_ratio(self):
        for platform in ["facebook", "linkedin_personal", "linkedin_company", "threads", "bluesky", "mastodon"]:
            with self.subTest(platform=platform):
                markup = VideoMarkup(self.preview(platform))
                self.assertIn("h-auto", markup.videos[0]["class"])

    def test_saved_and_pending_thumbnails_open_a_full_size_video(self):
        asset = SimpleNamespace(
            id=uuid4(), is_video=True, filename="clip.mp4", file=SimpleNamespace(url="/media/clip.mp4")
        )
        for template, context in [
            ("media_list.html", {"media_attachments": [SimpleNamespace(media_asset=asset)]}),
            ("media_list_pending.html", {"pending_assets": [asset], "workspace": SimpleNamespace(id=uuid4())}),
        ]:
            with self.subTest(template=template):
                markup = VideoMarkup(render_to_string("composer/partials/" + template, context))
                link = next(link for link in markup.links if link.get("href") == asset.file.url)
                self.assertEqual(link["aria-label"], "Play video: clip.mp4")
                self.assertEqual(link["target"], "_blank")
                self.assertIn("noopener", link["rel"])

    def test_approval_details_play_video_even_when_thumbnail_exists(self):
        asset = SimpleNamespace(
            is_video=True,
            filename="review.mp4",
            file=SimpleNamespace(url="/media/review.mp4"),
            thumbnail=SimpleNamespace(url="/media/poster.jpg"),
        )
        post = SimpleNamespace(
            title="Review clip",
            caption_snippet="Review this clip",
            author=SimpleNamespace(display_name="Reviewer"),
            id=uuid4(),
            media_attachments=SimpleNamespace(all=[SimpleNamespace(media_asset=asset)]),
            status="draft",
            platform_posts=SimpleNamespace(all=[]),
            versions=SimpleNamespace(all=[]),
            caption="Review this clip",
        )
        markup = VideoMarkup(
            render_to_string(
                "approvals/partials/_approval_row.html", {"post": post, "workspace": SimpleNamespace(id=uuid4())}
            )
        )
        self.assertEqual(len(markup.videos), 1)
        self.assertEqual(markup.videos[0]["src"], asset.file.url)
        self.assertIn("controls", markup.videos[0])
        self.assertEqual(markup.videos[0]["aria-label"], "Review video: review.mp4")
