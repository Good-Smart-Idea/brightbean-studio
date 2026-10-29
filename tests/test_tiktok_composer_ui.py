"""Execute the real Alpine privacy state and control bindings without a provider or DB."""

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class _TikTokControls(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls = []
        self.state = None
        self.effect = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "ttPrivacy:" in attrs.get("x-data", ""):
            self.state = attrs["x-data"]
            self.effect = attrs["x-effect"]
        self.controls.append({"tag": tag, **attrs})


def fixture(template):
    parser = _TikTokControls()
    parser.feed(template)
    return {"state": parser.state, "effect": parser.effect, "controls": parser.controls, "template": template}


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the isolated Alpine behavior suite")
class TikTokComposerUITests(unittest.TestCase):
    def test_actual_alpine_state_and_control_bindings(self):
        template = (ROOT / "templates/composer/compose.html").read_text()
        result = subprocess.run(
            ["node", str(ROOT / "tests/tiktok_composer_ui.test.cjs")],
            input=json.dumps(fixture(template)),
            text=True,
            capture_output=True,
            timeout=30,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
