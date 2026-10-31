"""Regression tests for the security and data bugs fixed after review."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tests  # noqa: F401  (sets up the test environment first)
from analytics import parse_post_time
from config import DASHBOARD_PORT, GROUPS_DIR, GroupFiles, group_slug, validate_settings
from dashboard import host_allowed
from import_session import is_facebook
from scraper import GroupRun, blocked, post_key
from utils import FIELDS, _append_csv, csv_safe, csv_unescape, read_rows

BASE = "2026-10-17T15:00:00"


class Security(unittest.TestCase):
    def test_dns_rebinding_host_is_rejected(self):
        self.assertTrue(host_allowed(f"localhost:{DASHBOARD_PORT}"))
        self.assertTrue(host_allowed(f"127.0.0.1:{DASHBOARD_PORT}"))
        self.assertFalse(host_allowed(f"evil.com:{DASHBOARD_PORT}"))
        self.assertFalse(host_allowed(""))

    def test_facebook_domain_boundary(self):
        self.assertTrue(is_facebook("facebook.com"))
        self.assertTrue(is_facebook(".facebook.com"))
        self.assertTrue(is_facebook("www.facebook.com"))
        self.assertFalse(is_facebook("notfacebook.com"))
        self.assertFalse(is_facebook("evilfacebook.com"))
        self.assertFalse(is_facebook("facebook.com.evil.example"))

    def test_group_slug_cannot_escape(self):
        self.assertEqual(group_slug("https://www.facebook.com/groups/.."), "group")
        self.assertEqual(group_slug("https://www.facebook.com/groups/."), "group")
        files = GroupFiles("https://www.facebook.com/groups/abc")
        self.assertEqual(os.path.commonpath([os.path.abspath(GROUPS_DIR), files.dir]),
                         os.path.abspath(GROUPS_DIR))

    def test_dot_group_url_is_rejected(self):
        _, errors = validate_settings({"GROUPS": ["https://www.facebook.com/groups/.."]})
        self.assertIn("GROUPS", errors)

    def test_keyword_phrases_keep_spaces(self):
        values, errors = validate_settings({"DEMAND_KEYWORDS": "looking for\nhow much"})
        self.assertEqual(errors, {})
        self.assertEqual(values["DEMAND_KEYWORDS"], ["looking for", "how much"])


class ScraperFixes(unittest.TestCase):
    class Page:
        def __init__(self, url):
            self.url = url

        def locator(self, *args, **kwargs):
            return SimpleNamespace(all_inner_texts=lambda: [])

    def test_group_named_login_is_not_a_login_redirect(self):
        self.assertFalse(blocked(self.Page("https://www.facebook.com/groups/login/")))
        self.assertFalse(blocked(self.Page("https://www.facebook.com/groups/checkpoint/")))
        self.assertTrue(blocked(self.Page("https://www.facebook.com/login.php")))
        self.assertTrue(blocked(self.Page("https://www.facebook.com/checkpoint/123")))

    def test_fallback_keys_do_not_collide(self):
        a = {"post_id": None, "author": "a", "time_text": "1h", "text": "same text"}
        b = {"post_id": None, "author": "b", "time_text": "1h", "text": "same text"}
        self.assertNotEqual(post_key(a), post_key(b))
        self.assertEqual(post_key(a), post_key(dict(a)))

    def test_legacy_hash_row_gets_numeric_url_without_duplicate_key(self):
        run = GroupRun.__new__(GroupRun)
        run.files = SimpleNamespace(url="https://www.facebook.com/groups/123")
        run.legacy_alias = {}
        run.urls_filled = 0
        run.legacy_index = {("author", "hello world this is a post"): "h_abc"}
        post = {"post_id": "999", "author": "author", "text": "hello   world this is a post"}
        with patch("scraper.anonymize", side_effect=lambda name: name):
            key = run.resolve_key(post)
        self.assertEqual(key, "h_abc")
        self.assertEqual(post["post_url"], "https://www.facebook.com/groups/123/posts/999")
        self.assertEqual(run.legacy_alias["999"], "h_abc")
        again = {"post_id": "999", "author": "author", "text": "hello   world this is a post"}
        self.assertEqual(run.resolve_key(again), "h_abc")
        self.assertEqual(again["post_url"], "https://www.facebook.com/groups/123/posts/999")

    def test_previous_missing_time_formats(self):
        cases = {
            "2 tháng": ("2026-08-18T15:00", False),
            "2 months": ("2026-08-18T15:00", False),
            "1 năm": ("2025-10-17T15:00", False),
            "Thứ Hai lúc 14:30": ("2026-10-12T14:30", True),
            "Monday at 2:30 PM": ("2026-10-12T14:30", True),
            "Hôm kia lúc 09:00": ("2026-10-15T09:00", True),
        }
        for text, want in cases.items():
            got, has_time = parse_post_time(text, BASE)
            self.assertEqual((got.isoformat(timespec="minutes"), has_time), want, text)


class CsvFixes(unittest.TestCase):
    def test_empty_file_gets_a_header(self):
        folder = tempfile.mkdtemp(prefix="fbscraper-csv-")
        path = os.path.join(folder, "posts.csv")
        open(path, "w", encoding="utf-8").close()
        _append_csv(path, FIELDS, [{"post_id": "1", "author": "a", "time_text": "t",
                                    "text": "hello", "scraped_at": "now"}])
        with open(path, encoding="utf-8-sig") as f:
            self.assertEqual(f.readline().strip().split(","), FIELDS)
        self.assertEqual(len(read_rows(path)), 1)

    def test_formula_cells_are_escaped_and_read_back(self):
        self.assertEqual(csv_safe("=1+1"), "'=1+1")
        self.assertEqual(csv_safe("+1"), "'+1")
        self.assertEqual(csv_safe("-1"), "'-1")
        self.assertEqual(csv_safe("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(csv_safe("normal"), "normal")
        self.assertEqual(csv_unescape("'=1+1"), "=1+1")
        self.assertEqual(csv_unescape("normal"), "normal")


if __name__ == "__main__":
    unittest.main()
