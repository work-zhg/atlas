import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import make_url  # noqa: E402
from report import anchors  # noqa: E402
from textkit import slugify  # noqa: E402


class TestStrings(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_url(self):
        self.assertEqual(make_url("A B"), "/posts/a-b")

    def test_anchors(self):
        self.assertEqual(anchors(["Intro Part"]), ["#intro-part"])
