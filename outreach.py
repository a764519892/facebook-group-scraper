"""Automatic replies to high-intent buyer posts, promoting the WandaLink shop.

Safety rules built in:
- only posts with a real permalink (numeric post id) are answered;
- only high-intent leads that match a product word are answered;
- a hard daily limit and long random gaps between comments;
- every post is answered at most once, tracked in data/outreach.json;
- consecutive failures or a Facebook checkpoint stop the whole run.

Run a preview without posting:

    python outreach.py --dry-run

The scraper calls run_after_scrape() automatically when AUTO_REPLY is on.
"""
import argparse
import json
import os
import random
import re
import time
from datetime import datetime, date

import analytics
from config import DATA_DIR, current_settings
from utils import log_message

STATE_FILE = os.path.join(DATA_DIR, "outreach.json")
PRODUCTS_CACHE = os.path.join(DATA_DIR, "products_cache.json")
SITE_URL = "https://handmade.wandalink.com/"
PRODUCTS_URL = SITE_URL + "api/products"

# Words that mean the post is already advertising our shop.
OWN_MARKERS = ("wandalink", "handmade.wandalink", "wanda link")

# Product choice: first matching rule wins. Keywords are lowercase Vietnamese.
PRODUCT_RULES = [
    (("kim móc", "móc"), "kim móc"),
    (("búp bê", "doll", "thú bông"), "búp bê"),
    (("nhung", "chenille", "gấu"), "nhung"),
    (("milk", "sữa", "cotton", "baby"), "sữa"),
    (("lông cừu", "wool"), "cừu"),
    (("tuyết", "ice"), "tuyết"),
    (("sợi dệt", "dệt"), "dệt"),
]

DEFAULT_TEMPLATES = [
    "Chào bạn, mình thấy bạn đang cần {product}. Bên mình có sẵn hàng, nhiều màu, giao nhanh trong nước. "
    "Bạn xem thử ở đây nhé: {url} — cần tư vấn thêm cứ nhắn mình nha.",
    "Bạn ơi, bên mình đang có {product} đúng loại bạn tìm. Màu và giá tham khảo tại {url}. "
    "Nếu bạn lấy số lượng nhiều, nhắn mình báo giá sỉ nhé.",
    "Hi bạn, mình có {product} nè. Bạn tham khảo ở {url} nha, có gì cần hỏi cứ nhắn mình.",
]


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state():
    state = _load_json(STATE_FILE, {})
    state.setdefault("commented", {})
    state.setdefault("failed", {})
    state.setdefault("daily", {})
    return state


def save_state(state):
    _save_json(STATE_FILE, state)


def load_products(force=False):
    """Website catalog, cached for a day. Returns [] if the site is unreachable."""
    cache = _load_json(PRODUCTS_CACHE, {})
    fetched = cache.get("fetched_at", "")
    items = cache.get("items") or []
    fresh = False
    try:
        fresh = (datetime.now() - datetime.fromisoformat(fetched)).total_seconds() < 86400
    except (TypeError, ValueError):
        pass
    if items and fresh and not force:
        return items
    try:
        import urllib.request
        req = urllib.request.Request(PRODUCTS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            items = json.loads(r.read().decode("utf-8"))
        _save_json(PRODUCTS_CACHE, {"fetched_at": datetime.now().isoformat(timespec="seconds"),
                                    "items": items})
    except Exception as e:
        log_message(f"Auto-reply: could not load the website catalog ({e}); using the cache.")
    return items


def _product_text(p):
    return " ".join(str(p.get(k) or "") for k in ("name", "sub", "category", "material")).lower()


# Product families: only offer what the post actually asks for. A finished doll
# is never offered to someone who only wants yarn, and vice versa.
FAMILY_KEYWORDS = {
    "kim móc": ("kim móc", "kim moc", "móc kim", "hook"),
    "búp bê": ("búp bê", "bup be", "thú bông", "thu bong", "doll"),
    "nhung": ("nhung", "chenille", "gấu", "gau"),
    "sữa": ("milk", "sữa", "sua", "cotton", "baby"),
    "cừu": ("lông cừu", "long cuu", "wool"),
    "tuyết": ("tuyết", "tuyet", "ice yarn"),
    "dệt": ("sợi dệt", "soi det", "dệt"),
}

FAMILY_PRODUCT_WORDS = {
    "kim móc": ("kim móc", "kim moc", "hook"),
    "búp bê": ("búp bê", "bup be", "doll", "thú"),
    "nhung": ("nhung", "chenille"),
    "sữa": ("sữa", "sua", "milk", "cotton", "baby"),
    "cừu": ("cừu", "cuu", "wool"),
    "tuyết": ("tuyết", "tuyet", "snow"),
    "dệt": ("dệt", "det"),
}


def detect_family(text):
    t = (text or "").lower()
    for family, words in FAMILY_KEYWORDS.items():
        if any(w in t for w in words):
            return family
    if "len" in t or "sợi" in t or "soi" in t:
        return "any"
    return None


def choose_product(post_text, products):
    """Pick the website product in the same family as the post. Prefer VN stock."""
    family = detect_family(post_text)
    if not family:
        return None
    words = FAMILY_PRODUCT_WORDS.get(family)
    scored = []
    for p in products:
        blob = _product_text(p)
        if words is None:  # generic yarn: never offer hooks or finished dolls
            if any(w in blob for w in ("kim móc", "búp bê", "doll")):
                continue
            score = 1
        else:
            score = sum(3 for w in words if w in blob)
            if not score:
                continue
        if p.get("supply_mode") == "vn_stock":
            score += 2
        scored.append((score, p))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], -int(x[1].get("lead_time_days") or 0)), reverse=True)
    return scored[0][1]


_BUYER_ASKS = [
    r"\bcần\s+(?:mua|tìm|tìm mua|order|đặt)\b",
    r"\b(?:tìm|muốn)\s+(?:mua|len|sợi|cotton|milk|nhung)\b",
    r"\bai\s+(?:bán|có)\b",
    r"\b(?:có ai|sốp nào|shop nào|chị nào|mẹ nào|bạn nào|nhà nào)\s+(?:bán|có|biết)\b",
    r"\b(?:chỗ bán|địa chỉ mua|mua ở đâu|ở đâu bán|tìm địa chỉ)\b",
    r"\bcần\s*\d",
    r"\b(?:order|looking for|want to buy|where to buy|who sells)\b",
]
_BUYER_RE = re.compile("|".join(_BUYER_ASKS), re.I)

# A price list or clearance post is a seller, not a buyer.
_SELLER_RE = re.compile(
    r"(xả lô|xả kho|xã kho|thanh lý|thanh lí|\bpass\b|bán sỉ|giá sỉ|\bsll\b|"
    r"nhận đơn|nhận móc|chốt đơn|mời các|link web|web em|qua web|"
    r"\d+\s*k\s*/\s*(?:cuộn|cuon|bó|bo|cái|cai|túi|tui|set|chiếc|chiec))", re.I)

# Someone complaining about a previous seller is not a good place to advertise:
# a promotional reply there reads as tone-deaf and often gets reported.
_COMPLAINT_RE = re.compile(
    r"(phốt|phot|tố cáo|to cao|lừa đảo|lua dao|bị lừa|bi lua|"
    r"từ chối|tu choi|hoàn tiền|hoan tien|trả hàng|tra hang|"
    r"không giống|khong giong|k giống|khác màu|khac mau|hàng lỗi|hang loi|"
    r"bực mình|buc minh|bức xúc|buc xuc|chửi|chui|đừng mua|dung mua)", re.I)


def is_buyer_post(text):
    """Strict gate for outreach: an explicit ask in a sentence that names a product.

    This keeps us out of seller price lists and posts that merely mention yarn in
    passing (for example a chart post).
    """
    t = text or ""
    if _SELLER_RE.search(t) or _COMPLAINT_RE.search(t):
        return False
    for sentence in re.split(r"[\n.!?。！？]+", t):
        if _BUYER_RE.search(sentence) and detect_family(sentence):
            return True
    return False


def _dedupe_key(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()



def compose_reply(post, product, settings, variant=None):
    url = (settings.get("AUTO_REPLY_SITE_URL") or SITE_URL).strip()
    templates = settings.get("AUTO_REPLY_TEMPLATES") or DEFAULT_TEMPLATES
    if isinstance(templates, str):
        templates = [templates]
    templates = [t for t in templates if t.strip()] or DEFAULT_TEMPLATES
    if variant is None:
        variant = random.randrange(len(templates))
    name = (product or {}).get("name") or "len và phụ kiện móc"
    name = re.split(r"\s*[|·]\s*", name)[0].strip()
    text = templates[variant % len(templates)]
    try:
        text = text.format(product=name, url=url)
    except (KeyError, IndexError):
        text = templates[0].format(product=name, url=url)
    quantity = bool(analytics._QUANTITY_RE.search(post.get("text", "") or ""))
    if quantity and "giá sỉ" not in text.lower():
        text += " Bên mình có giá sỉ cho đơn số lượng lớn nha."
    return text


def candidates(settings, products=None, days=None):
    """High-intent, product-matched posts with a real permalink, not yet answered."""
    products = products if products is not None else load_products()
    days = int(days or settings.get("AUTO_REPLY_DAYS") or 7)
    state = load_state()
    done = state["commented"]
    failed = state.get("failed", {})
    max_attempts = max(1, int(settings.get("AUTO_REPLY_MAX_ATTEMPTS") or 3))
    seen_text = set()
    out = []
    report = analytics.leads(settings, days=days, limit=500)
    for post in report["posts"]:
        if not post.get("url"):
            continue
        if not post.get("focus_hits"):
            continue
        text = post.get("text") or ""
        low = text.lower()
        if any(m in low for m in OWN_MARKERS):
            continue
        if not is_buyer_post(text):
            continue
        key = post.get("review_key") or analytics.review_key(post.get("group"), post.get("post_id"))
        if not key or key in done:
            continue
        record = failed.get(key) or {}
        if int(record.get("attempts") or 0) >= max_attempts:
            continue
        fingerprint = _dedupe_key(text)[:400]
        if fingerprint and fingerprint in seen_text:
            continue
        product = choose_product(text, products)
        if not product:
            continue
        if fingerprint:
            seen_text.add(fingerprint)
        out.append({**post, "key": key, "product": product,
                    "reply": compose_reply(post, product, settings)})
    out.sort(key=lambda p: (p.get("score", 0), p.get("scraped_at", "")), reverse=True)
    return out


def today_count(state=None):
    state = state if state is not None else load_state()
    return int(state.get("daily", {}).get(date.today().isoformat(), 0))


def record_success(key, post, reply):
    state = load_state()
    state["commented"][key] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "url": post.get("url", ""),
        "product": (post.get("product") or {}).get("name", ""),
        "reply": reply,
    }
    state["failed"].pop(key, None)
    day = date.today().isoformat()
    state["daily"][day] = int(state["daily"].get(day, 0)) + 1
    save_state(state)


def record_failure(key, error):
    state = load_state()
    old = state["failed"].get(key) or {}
    state["failed"][key] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "attempts": int(old.get("attempts") or 0) + 1,
        "error": str(error)[:300],
    }
    save_state(state)


def _blocked(page):
    url = (page.url or "").lower()
    if re.match(r"https?://[^/]+/(login|checkpoint|two_step_verification|recover)", url):
        return True
    try:
        body = page.locator("body").inner_text(timeout=2000).lower()
    except Exception:
        return False
    return any(s in body for s in ("tạm thời bị chặn", "temporarily blocked",
                                    "xác nhận bạn là con người", "confirm it's you"))


def _find_box(page):
    """Return the visible comment editor, or None."""
    scopes = [
        "div[role='textbox'][contenteditable='true']",
        "[contenteditable='true'][role='textbox']",
        "div[contenteditable='true'][aria-label*='bình luận' i]",
        "div[contenteditable='true'][aria-label*='comment' i]",
    ]
    for sel in scopes:
        loc = page.locator(sel)
        for i in range(loc.count() - 1, -1, -1):
            item = loc.nth(i)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
    return None


def _click_send(page):
    """Click the submit button that belongs to the comment editor.

    Facebook has used several shapes for this button (text "Bình luận"/"Đăng",
    an icon with only aria-label="Gửi", or aria-disabled="false"). Walk up from
    the editor and accept all of them.
    """
    return page.evaluate("""() => {
      const boxes = [...document.querySelectorAll(
        "div[role='textbox'][contenteditable='true'], [contenteditable='true'][role='textbox']"
      )].filter(e => e.offsetParent);
      const box = boxes[boxes.length - 1];
      if (!box) return false;
      const label = b => ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).trim();
      const isSend = b => /^(bình luận|binh luan|comment|đăng|dang|post|gửi|gui|send|trả lời|reply)$/i
        .test(label(b)) || /(đăng bình luận|post comment|gửi bình luận)/i.test(label(b));
      let root = box;
      for (let i = 0; i < 10 && root; i++) {
        root = root.parentElement;
        if (!root) break;
        const buttons = [...root.querySelectorAll("div[role='button'], button, [role='button']")]
          .filter(b => b.offsetParent && b !== box)
          .filter(b => (b.getAttribute('aria-disabled') || 'false') !== 'true');
        const hit = buttons.find(isSend);
        if (hit) { hit.click(); return true; }
      }
      return false;
    }""")


def post_comment(page, url, text):
    """Open a post, write the reply and press send. Returns (ok, message)."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(random.uniform(3.5, 6.0))
    if _blocked(page):
        return False, "Facebook login/checkpoint"
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        pass
    if any(s in body for s in ("đã tắt bình luận", "comments are turned off",
                                "comments are disabled")):
        return False, "comments are disabled"
    box = _find_box(page)
    if box is None:
        return False, "no comment box"
    box.click()
    time.sleep(random.uniform(0.6, 1.2))
    page.keyboard.insert_text(text)
    time.sleep(random.uniform(0.8, 1.5))
    if not _click_send(page):
        # Facebook's comment box also submits on Enter; this covers icon-only
        # send buttons that have no readable label.
        page.keyboard.press("Enter")
        time.sleep(random.uniform(1.0, 2.0))
    time.sleep(random.uniform(4.0, 6.5))
    if _blocked(page):
        return False, "Facebook login/checkpoint"
    # Facebook reformats whitespace and may collapse long comments, so accept
    # either a visible fragment of the reply or an emptied editor. Counting a
    # sent message as failed would make the next run post it again.
    try:
        snippet = re.sub(r"\s+", " ", text).strip()[:40]
        shown = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=4000))
        if snippet and snippet in shown:
            return True, "ok"
    except Exception:
        pass
    try:
        left = _find_box(page)
        editor = re.sub(r"\s+", " ", left.inner_text(timeout=2000)).strip() if left else ""
        if not editor:
            return True, "ok"
    except Exception:
        pass
    return False, "reply not visible after sending"


def run(context, settings=None, status=None, wait_fn=None, dry_run=False, log=log_message):
    """Reply to candidates in a browser context that is already logged in."""
    settings = settings or current_settings()
    state = load_state()
    daily_limit = max(1, int(settings.get("AUTO_REPLY_DAILY_LIMIT") or 5))
    remaining = daily_limit - today_count(state)
    if remaining <= 0:
        log(f"Auto-reply: daily limit reached ({daily_limit})")
        return {"sent": 0, "reason": "daily limit"}
    if dry_run:
        found = candidates(settings)
        for p in found:
            log(f"Auto-reply preview [{p['group']}] {p['url']}\n  {p['reply']}")
        return {"sent": 0, "preview": len(found)}

    products = load_products()
    found = candidates(settings, products)
    if not found:
        log("Auto-reply: no new high-intent posts to answer")
        return {"sent": 0, "reason": "no candidates"}

    min_gap = max(0.5, float(settings.get("AUTO_REPLY_MIN_MINUTES") or 3)) * 60
    max_gap = max(min_gap, float(settings.get("AUTO_REPLY_MAX_MINUTES") or 8) * 60)
    max_failures = max(1, int(settings.get("AUTO_REPLY_MAX_FAILURES") or 2))
    sent = failures = 0
    for n, post in enumerate(found[:remaining], 1):
        if status is not None:
            status.update(state="replying", message=f"自动回复 {n}/{min(len(found), remaining)}：{post['group']}")
            try:
                from utils import write_status
                write_status(status)
            except Exception:
                pass
        page = context.new_page()
        try:
            ok, message = post_comment(page, post["url"], post["reply"])
        except Exception as e:
            ok, message = False, f"error: {e}"
        finally:
            try:
                page.close()
            except Exception:
                pass
        if ok:
            sent += 1
            failures = 0
            record_success(post["key"], post, post["reply"])
            log(f"Auto-reply sent [{post['group']}] {post['url']}")
        else:
            failures += 1
            record_failure(post["key"], message)
            log(f"Auto-reply failed [{post['group']}] {post['url']}: {message}")
            if message == "Facebook login/checkpoint" or failures >= max_failures:
                log("Auto-reply stopped to protect the account")
                if status is not None:
                    status.update(state="done", message="自动回复已停止：Facebook 要求验证或连续失败")
                break
        if n < remaining and sent + failures < len(found):
            gap = random.uniform(min_gap, max_gap)
            log(f"Auto-reply: waiting {gap / 60:.1f} min before the next reply")
            if wait_fn is not None:
                if wait_fn(gap, status or {}, "自动回复：等待下一条") is False:
                    break
            else:
                time.sleep(gap)
    log(f"Auto-reply finished: {sent} sent, {failures} failed")
    return {"sent": sent, "failed": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Only print candidates and replies")
    args = parser.parse_args()
    settings = current_settings()
    if args.dry_run:
        run(None, settings, dry_run=True)
        return
    if not settings.get("AUTO_REPLY"):
        raise SystemExit("AUTO_REPLY is off. Turn it on in Settings first.")
    from secret_store import load_session, VaultError
    from utils import launch_browser
    from playwright.sync_api import sync_playwright
    try:
        session = load_session()
    except VaultError as e:
        raise SystemExit(str(e))
    with sync_playwright() as p:
        browser = launch_browser(p, False)
        context = browser.new_context(storage_state=session, locale="vi-VN",
                                      viewport={"width": 1100, "height": 850})
        try:
            run(context, settings)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
