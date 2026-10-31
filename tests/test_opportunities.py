"""Tests for the Opportunities / demand analysis tab."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import tests  # noqa: F401
import analytics
from utils import COMMENT_FIELDS, FIELDS, REACTION_FIELDS, _append_csv


class Opportunities(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="fbscraper-opps-")
        self.files = SimpleNamespace(
            slug="test-group", url="https://www.facebook.com/groups/test-group",
            posts=os.path.join(self.folder, "posts.csv"),
            comments=os.path.join(self.folder, "comments.csv"),
            reactions=os.path.join(self.folder, "reactions.csv"),
        )
        now = datetime.now()
        old = now - timedelta(days=40)
        _append_csv(self.files.posts, FIELDS, [
            {"post_id": "1", "author": "u1", "time_text": "2 giờ",
             "text": "谁有进口线？多少钱，求推荐", "scraped_at": now.isoformat(timespec="seconds")},
            {"post_id": "2", "author": "u2", "time_text": "3 ngày",
             "text": "出外贸线 现货 批发", "scraped_at": now.isoformat(timespec="seconds")},
            {"post_id": "3", "author": "u3", "time_text": "40 ngày",
             "text": "旧的帖子", "scraped_at": old.isoformat(timespec="seconds")},
        ])
        _append_csv(self.files.comments, COMMENT_FIELDS, [
            {"post_id": "1", "comment_index": 1, "author": "u4", "text": "我也要，多少钱",
             "scraped_at": now.isoformat(timespec="seconds")},
        ])
        _append_csv(self.files.reactions, REACTION_FIELDS, [
            {"post_id": "1", "total": 10, "comment_count": 5, "share_count": 2,
             "scraped_at": now.isoformat(timespec="seconds")},
            {"post_id": "2", "total": 2, "comment_count": 1, "share_count": 0,
             "scraped_at": now.isoformat(timespec="seconds")},
            {"post_id": "3", "total": 100, "comment_count": 100, "share_count": 100,
             "scraped_at": now.isoformat(timespec="seconds")},
        ])
        self.settings = {
            "OPPORTUNITY_DAYS": 30,
            "FOCUS_KEYWORDS": ["进口线", "外贸线"],
            "DEMAND_KEYWORDS": ["谁有", "多少钱", "求", "需要"],
            "SUPPLY_KEYWORDS": ["现货", "批发"],
            "IGNORE_KEYWORDS": [],
        }

    def test_recent_demand_and_supply_are_separated(self):
        report = analytics.opportunities(self.files, self.settings, days=30)
        self.assertEqual(report["summary"]["posts"], 2)  # the 40-day-old post is out
        self.assertEqual(report["summary"]["demand"], 1)
        self.assertEqual(report["summary"]["supply"], 1)
        by_id = {p["post_id"]: p for p in report["posts"]}
        self.assertIn("需求", by_id["1"]["tags"])
        self.assertIn("热帖", by_id["1"]["tags"])
        self.assertNotIn("供应", by_id["1"]["tags"])  # demand wins over seller words
        self.assertIn("供应", by_id["2"]["tags"])
        self.assertNotIn("需求", by_id["2"]["tags"])
        self.assertEqual(by_id["1"]["url"], "https://www.facebook.com/groups/test-group/posts/1")
        self.assertTrue(by_id["1"]["score"] > by_id["2"]["score"])

    def test_filters_and_export(self):
        demand = analytics.opportunities(self.files, self.settings, days=30, kind="demand")
        self.assertEqual([p["post_id"] for p in demand["posts"]], ["1"])
        supply = analytics.opportunities(self.files, self.settings, days=30, kind="supply")
        self.assertEqual([p["post_id"] for p in supply["posts"]], ["2"])
        csv_text = analytics.opportunities_csv(demand)
        self.assertIn("需求", csv_text)
        self.assertIn("谁有进口线", csv_text)
        self.assertTrue(csv_text.startswith("\ufeff"))

    def test_reviewed_marks_are_returned_and_filtered(self):
        marks = {"test-group:1": "2026-10-30T12:00:00"}
        report = analytics.opportunities(self.files, self.settings, days=30, reviewed=marks)
        by_id = {p["post_id"]: p for p in report["posts"]}
        self.assertEqual(by_id["1"]["review_key"], "test-group:1")
        self.assertEqual(by_id["1"]["reviewed_at"], marks["test-group:1"])
        hidden = analytics.opportunities(
            self.files, self.settings, days=30, reviewed=marks, hide_reviewed=True)
        self.assertNotIn("1", [p["post_id"] for p in hidden["posts"]])

        posts = analytics.list_posts(self.files, reviewed=marks)
        listed = {p["post_id"]: p for p in posts["posts"]}
        self.assertEqual(listed["1"]["reviewed_at"], marks["test-group:1"])
        remaining = analytics.list_posts(self.files, reviewed=marks, hide_reviewed=True)
        self.assertNotIn("1", [p["post_id"] for p in remaining["posts"]])

    def test_formula_text_is_exported_safely(self):
        _append_csv(self.files.posts, FIELDS, [{
            "post_id": "4", "author": "u4", "time_text": "1 giờ", "text": "=HYPERLINK(evil)",
            "post_url": "", "scraped_at": datetime.now().isoformat(timespec="seconds"),
        }])
        report = analytics.opportunities(self.files, self.settings, days=30)
        text = analytics.opportunities_csv(report)
        self.assertIn("'=HYPERLINK(evil)", text)

    def test_chinese_topics_are_useful(self):
        report = analytics.opportunities(self.files, self.settings, days=30)
        topics = dict(report["summary"]["top_topics"])
        self.assertIn("进口线", topics)


class WandalinkDemand(unittest.TestCase):
    """Yarn/crochet classification: product gate, seller blockers, lead links."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="fbscraper-yarn-")
        self.files = SimpleNamespace(
            slug="yarn-group", url="https://www.facebook.com/groups/yarn-group",
            posts=os.path.join(self.folder, "posts.csv"),
            comments=os.path.join(self.folder, "comments.csv"),
            reactions=os.path.join(self.folder, "reactions.csv"),
        )
        now = datetime.now().isoformat(timespec="seconds")
        _append_csv(self.files.posts, FIELDS, [
            {"post_id": "lead", "author": "u1", "time_text": "1 giờ",
             "text": "Tìm địa chỉ mua len milk cotton 50gr và nhung gấu", "scraped_at": now},
            {"post_id": "qty", "author": "u2", "time_text": "2 giờ",
             "text": "M cần 50c milk 50g ai nhận giá tốt", "scraped_at": now},
            {"post_id": "generic", "author": "u3", "time_text": "3 giờ",
             "text": "Mình cần 50c đỏ, ai có nhận", "scraped_at": now},
            {"post_id": "seller", "author": "u4", "time_text": "4 giờ",
             "text": "PASS LEN NHUNG ĐŨA, bạn nào cần mua nguyên liệu thì chốt đơn nhé", "scraped_at": now},
            {"post_id": "seller2", "author": "u5", "time_text": "5 giờ",
             "text": "Nấm 10k tặng kèm móc khóa SẴN SLL, Ib để đặt hàng", "scraped_at": now},
            {"post_id": "shop-lead", "author": "u6", "time_text": "6 giờ",
             "text": "Shop nào bán len nhung gấu giá tốt?", "scraped_at": now},
        ])
        self.settings = {
            "OPPORTUNITY_DAYS": 30,
            "FOCUS_KEYWORDS": ["len", "milk", "nhung", "nhung gấu"],
            "DEMAND_KEYWORDS": ["cần", "mua", "tìm", "cần mua", "tìm địa chỉ", "đặt hàng"],
            "STRONG_DEMAND_KEYWORDS": ["tìm địa chỉ", "shop nào"],
            "SELLER_BLOCKERS": ["pass", "chốt đơn", "sẵn", "sll", "ib"],
            "SUPPLY_KEYWORDS": ["pass", "sẵn", "sll", "ib"],
            "IGNORE_KEYWORDS": [],
        }

    def test_product_gate_and_seller_blockers(self):
        report = analytics.opportunities(self.files, self.settings, days=30)
        by_id = {p["post_id"]: p for p in report["posts"]}
        self.assertTrue(by_id["lead"]["demand_ok"])
        self.assertTrue(by_id["lead"]["high_intent"])
        self.assertTrue(by_id["qty"]["high_intent"])  # quantity signal
        self.assertFalse(by_id["generic"]["demand_ok"])  # no yarn product word
        self.assertFalse(by_id["seller"]["demand_ok"])  # seller blocker suppresses "cần mua"
        self.assertFalse(by_id["seller2"]["demand_ok"])
        self.assertTrue(by_id["shop-lead"]["demand_ok"])  # buyer phrase overrides "shop"
        self.assertEqual(report["summary"]["leads"], 3)

    def test_lead_filter_and_search_link(self):
        leads = analytics.opportunities(self.files, self.settings, days=30, kind="lead")
        self.assertEqual({p["post_id"] for p in leads["posts"]}, {"lead", "qty", "shop-lead"})
        lead = next(p for p in leads["posts"] if p["post_id"] == "lead")
        self.assertTrue(lead["search_url"].startswith(
            "https://www.facebook.com/groups/yarn-group/search/?q="))
        csv_text = analytics.opportunities_csv(leads)
        self.assertIn("search_url", csv_text)
        self.assertIn("high_intent", csv_text)

    def test_cross_group_leads(self):
        settings = {**self.settings, "GROUPS": ["https://facebook.com/groups/a",
                                               "https://facebook.com/groups/b"]}
        with patch("analytics.GroupFiles", return_value=self.files):
            report = analytics.leads(settings, days=30, limit=100)
        self.assertEqual(report["summary"]["groups"], 2)
        self.assertEqual(report["total"], 6)  # 3 high-intent posts per group call


if __name__ == "__main__":
    unittest.main()
