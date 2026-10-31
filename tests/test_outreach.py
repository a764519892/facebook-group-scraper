"""Tests for the automatic website-promotion replies (no Facebook, no network)."""
import os
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401
import outreach
from config import current_settings


PRODUCTS = [
    {"name": "Len nhung Susan's Family", "sub": "Len mềm", "material": "Sợi nhung (chenille)",
     "category": "Sợi len", "supply_mode": "vn_stock", "lead_time_days": 0},
    {"name": "Len sữa Wenmeishu 4 sợi", "sub": "milk cotton", "material": "Sợi sữa",
     "category": "Sợi len", "supply_mode": "china_preorder", "lead_time_days": 15},
    {"name": "Kim móc Thành Tín", "sub": "kim móc", "material": "", "category": "Kim móc",
     "supply_mode": "vn_stock", "lead_time_days": 0},
    {"name": "Thỏ handmade váy hồng", "sub": "búp bê", "material": "", "category": "Búp bê móc len",
     "supply_mode": "vn_stock", "lead_time_days": 0},
]


class BuyerGate(unittest.TestCase):
    def test_explicit_buyer_asks_pass(self):
        for text in [
            "Mình cần 30 cuộn len 50g, cần sợi mập. Sốp nào có không ạ.",
            "Em tìm len milk cotton 50gr ở HN ạ",
            "Ai bán charm noel k ạ, cho e hỏi len ss4",
            "looking for milk cotton yarn",
        ]:
            self.assertTrue(outreach.is_buyer_post(text), text)

    def test_sellers_and_charts_are_rejected(self):
        for text in [
            "CHART MÓC VỊT QUẢ LÊ | Len: Susan family 4 | Kim móc: 2.0mm",
            "[CẬN CẢNH LEN 125GRAM] Nhiều chị cần xem cận chất len",
            "XẢ LÔ LEN NHUNG ĐŨA chỉ 16K/CUỘN",
            "Sợi dệt 2mm chuyên túi nón ví | Giá mua chẵn 100k lẻ 25 1 cuộn",
            "Mình pass 3 cuộn len nhung",
        ]:
            self.assertFalse(outreach.is_buyer_post(text), text)


class ProductChoice(unittest.TestCase):
    def test_yarn_buyer_never_gets_a_doll_or_hook(self):
        product = outreach.choose_product("Em tìm len milk cotton 50gr", PRODUCTS)
        self.assertEqual(product["name"], "Len sữa Wenmeishu 4 sợi")
        product = outreach.choose_product("Cần 30 cuộn len nhung", PRODUCTS)
        self.assertEqual(product["name"], "Len nhung Susan's Family")
        product = outreach.choose_product("Tìm mua kim móc 3mm", PRODUCTS)
        self.assertEqual(product["name"], "Kim móc Thành Tín")
        product = outreach.choose_product("Mình cần búp bê móc len", PRODUCTS)
        self.assertEqual(product["name"], "Thỏ handmade váy hồng")

    def test_unrelated_post_gets_no_product(self):
        self.assertIsNone(outreach.choose_product("Ai bán charm noel k ạ", PRODUCTS))

    def test_local_stock_wins_on_a_tie(self):
        products = [
            {"name": "Len nhung A", "sub": "", "material": "chenille", "category": "Sợi len",
             "supply_mode": "china_preorder", "lead_time_days": 15},
            {"name": "Len nhung B", "sub": "", "material": "chenille", "category": "Sợi len",
             "supply_mode": "vn_stock", "lead_time_days": 0},
        ]
        self.assertEqual(outreach.choose_product("cần len nhung", products)["name"], "Len nhung B")


class ReplyCopy(unittest.TestCase):
    def test_reply_uses_product_and_site_url(self):
        post = {"text": "Mình cần 30 cuộn len nhung"}
        reply = outreach.compose_reply(post, PRODUCTS[0], current_settings(), variant=1)
        self.assertIn("https://handmade.wandalink.com/", reply)
        self.assertIn("Len nhung Susan's Family", reply)
        self.assertIn("giá sỉ", reply)

    def test_missing_placeholders_do_not_crash(self):
        settings = {**current_settings(), "AUTO_REPLY_TEMPLATES": ["Xin chào"]}
        reply = outreach.compose_reply({"text": "cần len"}, PRODUCTS[0], settings, variant=0)
        self.assertEqual(reply, "Xin chào")


class Sending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbscraper-outreach-")
        self.state = os.path.join(self.tmp, "outreach.json")
        self.patches = [patch.object(outreach, "STATE_FILE", self.state)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_posts_are_never_answered_twice(self):
        post = {"group": "123", "post_id": "999", "url": "https://facebook.com/x", "product": PRODUCTS[0]}
        outreach.record_success("123:999", post, "hello")
        state = outreach.load_state()
        self.assertIn("123:999", state["commented"])
        self.assertEqual(outreach.today_count(state), 1)
        outreach.record_failure("123:1000", "no comment box")
        self.assertIn("123:1000", outreach.load_state()["failed"])

    def test_preview_never_posts(self):
        settings = current_settings()
        with patch.object(outreach, "candidates", return_value=[
            {"group": "1", "post_id": "2", "url": "u", "reply": "r", "product": PRODUCTS[0]}]):
            result = outreach.run(None, settings, dry_run=True, log=lambda *_: None)
        self.assertEqual(result["preview"], 1)
        self.assertEqual(result["sent"], 0)


if __name__ == "__main__":
    unittest.main()
