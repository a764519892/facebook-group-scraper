"""Tests for the persistent 'already reviewed' post marks."""
import os
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401
import reviewed


class Reviewed(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="fbscraper-reviewed-")
        self.path = os.path.join(self.folder, "reviewed.json")
        self.patch = patch.object(reviewed, "REVIEWED_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_mark_annotate_and_unmark(self):
        key = reviewed.review_key("group-a", "123")
        self.assertEqual(key, "group-a:123")

        at = reviewed.set_state(key, True)
        self.assertTrue(at)
        self.assertEqual(reviewed.load()[key], at)

        posts = reviewed.annotate([{"post_id": "123"}], group="group-a")
        self.assertEqual(posts[0]["review_key"], key)
        self.assertEqual(posts[0]["reviewed_at"], at)

        reviewed.set_state(key, False)
        self.assertNotIn(key, reviewed.load())
        self.assertEqual(reviewed.set_state(key, False), "")

    def test_bad_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            reviewed.set_state("", True)
        with self.assertRaises(ValueError):
            reviewed.set_state("x" * 301, True)
