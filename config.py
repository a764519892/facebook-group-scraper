import json
import os
import re

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Everything the scraper writes lives here (mount this folder as a volume in Docker)
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data")))
# Each group gets its own folder: data/groups/<slug>/posts.csv, comments.csv, reactions.csv
GROUPS_DIR = os.path.join(DATA_DIR, "groups")
# Last checkpoint Facebook showed, for the dashboard warning
CHECKPOINT_FILE = os.path.join(DATA_DIR, "checkpoint.json")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
LOG_FILE = os.path.join(DATA_DIR, "logs.txt")
# Settings changed from the dashboard; they win over .env
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
# Created by the dashboard's Stop button; the scraper stops at the next scroll
STOP_FILE = os.path.join(DATA_DIR, "stop.flag")
# One scrape at a time, whether started from the dashboard or a terminal
RUN_LOCK_FILE = os.path.join(DATA_DIR, "run.lock")
# Posts you already reviewed, so they can be dimmed/hidden next time
REVIEWED_FILE = os.path.join(DATA_DIR, "reviewed.json")
# Passphrase-encrypted email, password and login session
VAULT_FILE = os.path.join(DATA_DIR, "vault.enc")

os.makedirs(DATA_DIR, exist_ok=True)

# What low-risk mode enforces, whatever the other settings say
LOW_RISK_MODE_MAX_POSTS = 30
LOW_RISK_MODE_MIN_DELAY = 5
LOW_RISK_MODE_MAX_DELAY = 12

GROUP_URL = re.compile(r"^https://(www\.|m\.)?facebook\.com/groups/(?!\.{1,2}/?$)[^/?#\s]+/?$")
# Placeholder: put your own group(s) in Settings or .env
DEFAULT_GROUP = "https://www.facebook.com/groups/your-group-name"
MAX_GROUPS = 20


def group_slug(url):
    """Folder name for a group: the part after /groups/ (or the file name for local test pages)."""
    m = re.search(r"/groups/([^/?#]+)", url or "")
    name = m.group(1) if m else os.path.splitext(os.path.basename((url or "").rstrip("/")))[0]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "group"
    # Dots are legal in a group name, but "." and ".." would escape GROUPS_DIR
    return "group" if name in (".", "..") else name


class GroupFiles:
    """Where one group's data lives."""

    def __init__(self, url):
        self.url = url
        self.slug = group_slug(url)
        self.dir = os.path.abspath(os.path.join(GROUPS_DIR, self.slug))
        # Defense in depth: never read or write outside data/groups/
        root = os.path.abspath(GROUPS_DIR)
        if os.path.commonpath([root, self.dir]) != root:
            raise ValueError(f"Unsafe group folder: {self.slug!r}")
        self.posts = os.path.join(self.dir, "posts.csv")
        self.comments = os.path.join(self.dir, "comments.csv")
        self.reactions = os.path.join(self.dir, "reactions.csv")

    def __repr__(self):
        return f"GroupFiles({self.slug})"

# Settings the dashboard can edit. Each value comes from settings.json, else
# .env / environment, else the default here. "disabled_when" greys a field out
# in the dashboard while another setting has the given value; "note_when" shows
# a hint under the field instead (e.g. what low-risk mode overrides).
SCHEMA = {
    "GROUPS": dict(type="list", default=[DEFAULT_GROUP], group="群组",
                   label=f"要抓取的群组，每行一个链接（最多 {MAX_GROUPS} 个）", pattern=GROUP_URL),
    # Picked from the header's group menu, not shown as a field
    "SELECTED_GROUP": dict(type="str", default="", group="群组", label="当前群组", hidden=True),
    "SCRAPE_ALL_GROUPS": dict(type="bool", default=False, group="群组",
                              label="点“开始抓取”时，依次抓取列表里的所有群组"),
    "GROUP_PAUSE_MIN": dict(type="float", default=3, min=0.5, max=60, group="群组",
                            label="群组之间暂停（分钟）", disabled_when=[["SCRAPE_ALL_GROUPS", False]]),
    # Newest posts first instead of Facebook's "relevant" ordering
    "SORT_CHRONOLOGICAL": dict(type="bool", default=True, group="群组", label="按最新帖子排序"),

    "LOW_RISK_MODE": dict(type="bool", default=False, group="安全",
                       label=f"低风险模式：每次最多 {LOW_RISK_MODE_MAX_POSTS} 条、"
                             f"暂停 {LOW_RISK_MODE_MIN_DELAY}-{LOW_RISK_MODE_MAX_DELAY} 秒、不额外点击"),
    "CHECKPOINT_ALERT": dict(type="bool", default=True, group="安全",
                             label="Facebook 出现验证/检查点时提醒我"),

    "MAX_POSTS": dict(type="int", default=100, min=1, max=500, group="频率与数量",
                      label="每次最多抓取多少条新帖",
                      note_when=[["LOW_RISK_MODE", True, f"低风险模式下最多 {LOW_RISK_MODE_MAX_POSTS} 条"],
                                 ["BATCH_MODE", True, "分批模式下不使用，请看“速度与分批”"]],
                      disabled_when=[["BATCH_MODE", True]]),
    # Each run scrolls past posts saved earlier; auto mode sizes the run from
    # how many are saved and how fast the feed is loading
    "AUTO_SCROLL_LIMIT": dict(type="bool", default=True, group="频率与数量",
                              label="自动增加滚动次数，以到达还没保存的帖子"),
    "SCROLL_LIMIT": dict(type="int", default=40, min=1, max=300, group="频率与数量",
                         label="每次最多滚动次数（关闭自动时）",
                         disabled_when=[["AUTO_SCROLL_LIMIT", True]]),
    "MAX_IDLE_SCROLLS": dict(type="int", default=4, min=1, max=20, group="频率与数量",
                             label="连续 N 次滚动都没有新内容就停止"),
    "MIN_DELAY": dict(type="float", default=3, min=1, max=60, group="频率与数量",
                      label="两次滚动之间最短暂停（秒）",
                      note_when=[["LOW_RISK_MODE", True, f"低风险模式下至少 {LOW_RISK_MODE_MIN_DELAY} 秒"]]),
    "MAX_DELAY": dict(type="float", default=7, min=1, max=120, group="频率与数量",
                      label="两次滚动之间最长暂停（秒）",
                      note_when=[["LOW_RISK_MODE", True, f"低风险模式下至少 {LOW_RISK_MODE_MAX_DELAY} 秒"]]),

    # Each run starts at the newest post and has to pass the saved ones first
    "FAST_FORWARD": dict(type="bool", default=True, group="速度与分批",
                         label="a. 快速滑过已保存的帖子（暂停 1-2 秒）"),
    "SKIP_SEE_MORE_SAVED": dict(type="bool", default=True, group="速度与分批",
                                label="b. 已保存的帖子不点“查看更多”"),
    "BATCH_MODE": dict(type="bool", default=False, group="速度与分批",
                       label="c. 分批抓取：批与批之间休息，页面保持打开，然后从原位置继续"),
    "BATCH_POSTS": dict(type="int", default=30, min=1, max=500, group="速度与分批",
                        label="每批抓取多少条新帖", disabled_when=[["BATCH_MODE", False]],
                        note_when=[["LOW_RISK_MODE", True, f"低风险模式下最多 {LOW_RISK_MODE_MAX_POSTS} 条"]]),
    "BATCH_TOTAL": dict(type="int", default=150, min=1, max=5000, group="速度与分批",
                        label="总共抓取多少条新帖后停止", disabled_when=[["BATCH_MODE", False]]),
    "BATCH_REST_MIN": dict(type="float", default=120, min=5, max=1440, group="速度与分批",
                           label="每批之间休息（分钟）", disabled_when=[["BATCH_MODE", False]]),

    # Posts show 1-2 comments in the feed; to reach the limit the scraper
    # clicks "View more comments" once per post
    "COLLECT_COMMENTS": dict(type="bool", default=True, group="评论", label="抓取评论"),
    "MAX_COMMENTS_PER_POST": dict(type="int", default=3, min=1, max=5, group="评论",
                                  label="每条帖子抓取几条评论（1-5）", disabled_when=[["COLLECT_COMMENTS", False]]),
    "EXPAND_COMMENTS": dict(type="bool", default=True, group="评论",
                            label="需要时点击“查看更多评论”",
                            disabled_when=[["COLLECT_COMMENTS", False], ["LOW_RISK_MODE", True]]),

    # The bar under a post shows the top 3 reaction types; the popup has every type
    "COLLECT_REACTIONS": dict(type="bool", default=True, group="互动数据", label="抓取互动（点赞等）"),
    "EXPAND_REACTIONS": dict(type="bool", default=True, group="互动数据",
                             label="打开互动弹窗，读取每种类型的数量",
                             disabled_when=[["COLLECT_REACTIONS", False], ["LOW_RISK_MODE", True]]),

    # Demand/opportunity analysis. These are keyword lists, not Facebook groups,
    # so spaces inside a term are preserved (e.g. "looking for").
    "OPPORTUNITY_DAYS": dict(type="int", default=30, min=1, max=3650, group="需求词设置",
                             label="“需求机会”默认查看最近多少天"),
    "FOCUS_KEYWORDS": dict(type="list", split=r"[\n,]+",
                           default=["len", "sợi", "milk", "milk cotton", "cotton", "nhung",
                                    "nhung gấu", "chenille", "baby", "baby yarn", "sữa", "len sữa",
                                    "lông cừu", "dệt kim", "dệt trơn", "kim tuyến", "cloudcandy",
                                    "loang", "kim móc", "búp bê"],
                           group="需求词设置",
                           label="重点产品词（毛线/钩织材料；每行一个，越南语或英语都可以）"),
    "DEMAND_KEYWORDS": dict(type="list", split=r"[\n,]+",
                            default=["cần", "mua", "tìm", "cần mua", "cần tìm", "muốn mua", "tìm mua",
                                     "mua len", "mua sợi", "tìm len", "tìm sợi", "ai có", "ai bán",
                                     "có ai bán", "chỗ bán", "tìm địa chỉ", "địa chỉ mua",
                                     "mua ở đâu", "ở đâu bán", "cho hỏi", "hỏi giá", "báo giá", "xin giá",
                                     "giá bao nhiêu", "bao nhiêu tiền", "cần số lượng", "cần sl",
                                     "cần sll", "cần hàng", "đặt hàng", "order", "need", "looking for",
                                     "want to buy", "where to buy", "anyone sell", "who sells"],
                            group="需求词设置",
                            label="买家需求词（会和产品词、卖家词一起判断，不会单独算需求）"),
    "DEMAND_REQUIRES_FOCUS": dict(type="bool", default=True, group="需求词设置",
                                  label="需求必须同时命中产品词（推荐开启，避免把“cần/mua”误判）"),
    "STRONG_DEMAND_KEYWORDS": dict(type="list", split=r"[\n,]+",
                                   default=["muốn mua", "tìm mua",
                                            "tìm len", "tìm sợi", "ai bán", "có ai bán",
                                            "chỗ bán", "tìm địa chỉ", "địa chỉ mua", "mua ở đâu",
                                            "ở đâu bán", "shop nào", "order len", "order sợi"],
                                   group="需求词设置",
                                   label="强买家短语（出现这些词时，即使有卖家词也判为需求）"),
    "SUPPLY_KEYWORDS": dict(type="list", split=r"[\n,]+",
                            default=["bán", "bán sỉ", "bán buôn", "cung cấp", "sỉ", "giá sỉ",
                                     "nhập khẩu", "hàng có sẵn", "còn hàng", "đại lý", "nhà cung cấp",
                                     "pass", "thanh lý", "thanh lí", "xả kho", "xã kho", "sẵn", "sll",
                                     "shop", "shopee", "nhận đơn", "nhận móc", "chốt đơn", "mời các", "sale",
                                     "inbox", "nhắn tin",
                                     "sell", "for sale", "supply", "wholesale", "in stock",
                                     "agent", "vendor", "distributor"],
                            group="需求词设置",
                            label="卖家/同行词（命中后标为“供应”，用于了解行情）"),
    "SELLER_BLOCKERS": dict(type="list", split=r"[\n,]+",
                            default=["pass", "thanh lý", "thanh lí", "xả kho", "xã kho", "xả hàng",
                                     "nhận đơn", "nhận móc", "nhận sỉ", "giá sỉ", "bán sỉ", "bán buôn",
                                     "cung cấp", "đại lý", "hàng có sẵn", "sẵn sll", "sẵn số lượng",
                                     "sẵn kho", "sẵn hàng", "sẵn", "shop", "shopee", "ib", "inbox",
                                     "nhắn em", "nhắn tin", "link web", "web em", "ưu đãi", "qua web",
                                     "chốt đơn", "mời các",
                                     "sale", "khuyến mãi", "combo", "ship cod"],
                            group="需求词设置",
                            label="卖家/清仓词（帖子正文含这些词时不标为需求）"),
    "IGNORE_KEYWORDS": dict(type="list", split=r"[\n,]+",
                            default=["tuyển dụng", "việc làm", "tuyển", "hiring", "job", "recruitment",
                                     "lens", "kính", "mắt kính"],
                            group="需求词设置",
                            label="忽略包含这些词的帖子（可选，例如“tuyển dụng”“lens”）"),

    # Posts saved in earlier runs scroll past again; their counts can be refreshed
    "UPDATE_SAVED": dict(type="bool", default=False, group="已保存帖子",
                         label="滚动经过已保存帖子时，更新互动和评论"),
    "UPDATE_SAVED_CLICKS": dict(type="bool", default=False, group="已保存帖子",
                                label="已保存帖子也打开弹窗（每条最多多 2 次点击）",
                                disabled_when=[["UPDATE_SAVED", False], ["LOW_RISK_MODE", True]]),

    # Memory: Facebook's feed keeps images and videos loaded as you scroll
    "BLOCK_MEDIA": dict(type="bool", default=True, group="性能",
                        label="不加载图片、视频和字体（省内存和流量）"),
    "RECYCLE_MB": dict(type="int", default=1200, min=0, max=8000, group="性能",
                       label="页面内存超过多少 MB 时重新打开（0 = 不重开）"),

    "BROWSER_CHANNEL": dict(type="choice", default="", choices=["", "chromium", "chrome", "msedge"],
                            group="浏览器", label="浏览器"),
    "HEADLESS": dict(type="bool", default=False, group="浏览器", label="隐藏浏览器窗口"),

    # Replace author names with a salted hash so the dataset holds no names
    "ANONYMIZE": dict(type="bool", default=True, group="隐私",
                      label="保存前哈希作者名（建议保持开启）",
                      note_when=[["ANONYMIZE", False, "会保存真实姓名。只有获得对方同意才关闭。"]]),

    # Automatic replies that promote the website on high-intent buyer posts
    "AUTO_REPLY": dict(type="bool", default=False, group="自动回复",
                       label="抓取结束后，自动回复高意向买家帖（宣传网站）"),
    "AUTO_REPLY_DAILY_LIMIT": dict(type="int", default=5, min=1, max=50, group="自动回复",
                                   label="每天最多自动回复几条（建议 5-10）",
                                   disabled_when=[["AUTO_REPLY", False]]),
    "AUTO_REPLY_MIN_MINUTES": dict(type="float", default=3, min=0.5, max=120, group="自动回复",
                                   label="两条回复之间最短等待（分钟）",
                                   disabled_when=[["AUTO_REPLY", False]]),
    "AUTO_REPLY_MAX_MINUTES": dict(type="float", default=8, min=1, max=240, group="自动回复",
                                   label="两条回复之间最长等待（分钟）",
                                   disabled_when=[["AUTO_REPLY", False]]),
    "AUTO_REPLY_DAYS": dict(type="int", default=7, min=1, max=3650, group="自动回复",
                            label="只回复最近多少天内的买家帖",
                            disabled_when=[["AUTO_REPLY", False]]),
    "AUTO_REPLY_MAX_FAILURES": dict(type="int", default=2, min=1, max=10, group="自动回复",
                                    label="连续失败几次就停止（保护账号）",
                                    disabled_when=[["AUTO_REPLY", False]]),
    "AUTO_REPLY_SITE_URL": dict(type="str", default="https://handmade.wandalink.com/", group="自动回复",
                                label="回复里附带的网站地址",
                                disabled_when=[["AUTO_REPLY", False]]),
    "AUTO_REPLY_TEMPLATES": dict(type="list", split=r"\n+", default=[
        "Chào bạn, mình thấy bạn đang cần {product}. Bên mình có sẵn hàng, nhiều màu, giao nhanh trong nước. "
        "Bạn xem thử ở đây nhé: {url} — cần tư vấn thêm cứ nhắn mình nha.",
        "Bạn ơi, bên mình đang có {product} đúng loại bạn tìm. Màu và giá tham khảo tại {url}. "
        "Nếu bạn lấy số lượng nhiều, nhắn mình báo giá sỉ nhé.",
        "Hi bạn, mình có {product} nè. Bạn tham khảo ở {url} nha, có gì cần hỏi cứ nhắn mình.",
    ], group="自动回复",
        label="回复文案（每行一条，随机使用；{product} 和 {url} 会自动替换）",
        disabled_when=[["AUTO_REPLY", False]]),
}


def _convert(key, raw):
    spec = SCHEMA[key]
    kind = spec["type"]
    if kind == "bool":
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(float(raw))
    if kind == "float":
        return float(raw)
    if kind == "list":
        items = raw if isinstance(raw, list) else re.split(spec.get("split", r"[\s,]+"), str(raw))
        out = []
        for item in (str(i).strip().rstrip("/") for i in items):
            if item and item not in out:
                out.append(item)
        return out
    return str(raw).strip()


def _read_settings_file():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def current_settings():
    saved = _read_settings_file()
    # Older versions had one TARGET_GROUP instead of a list
    legacy_group = saved.get("TARGET_GROUP", os.getenv("TARGET_GROUP"))
    if legacy_group and "GROUPS" not in saved and os.getenv("GROUPS") is None:
        saved["GROUPS"] = [legacy_group]
    # Older versions called low-risk mode "light mode"
    legacy = saved.get("LIGHT_MODE", os.getenv("LIGHT_MODE"))
    if legacy is not None and "LOW_RISK_MODE" not in saved and os.getenv("LOW_RISK_MODE") is None:
        saved["LOW_RISK_MODE"] = legacy
    # Older versions turned comments off with "0 comments per post"
    if str(saved.get("MAX_COMMENTS_PER_POST", os.getenv("MAX_COMMENTS_PER_POST"))) == "0"             and "COLLECT_COMMENTS" not in saved and os.getenv("COLLECT_COMMENTS") is None:
        saved["COLLECT_COMMENTS"] = False
    values = {}
    for key, spec in SCHEMA.items():
        raw = saved.get(key, os.getenv(key))
        try:
            value = spec["default"] if raw is None or raw == "" and spec["type"] != "choice" else _convert(key, raw)
        except (TypeError, ValueError):
            value = spec["default"]
        if spec["type"] in ("int", "float"):
            value = max(spec["min"], min(spec["max"], value))
        if spec["type"] == "choice" and value not in spec["choices"]:
            value = spec["default"]
        if spec["type"] == "list" and not value:
            value = list(spec["default"])
        values[key] = value
    # The selected group must be one of the list
    if values["SELECTED_GROUP"] not in values["GROUPS"]:
        values["SELECTED_GROUP"] = values["GROUPS"][0]
    return values


def validate_settings(updates):
    """Return (clean_values, errors) for a dict of submitted settings."""
    clean, errors = {}, {}
    for key, raw in updates.items():
        if key not in SCHEMA:
            continue
        spec = SCHEMA[key]
        try:
            value = _convert(key, raw)
        except (TypeError, ValueError):
            errors[key] = "请输入有效数字"
            continue
        if spec["type"] in ("int", "float") and not spec["min"] <= value <= spec["max"]:
            errors[key] = f"必须在 {spec['min']} 和 {spec['max']} 之间"
        elif spec["type"] == "choice" and value not in spec["choices"]:
            errors[key] = "选项无效"
        elif spec["type"] == "list" and "pattern" in spec:
            bad = [v for v in value if not spec["pattern"].match(v)]
            if not value:
                errors[key] = "至少添加一个群组"
            elif bad:
                errors[key] = f"不是有效的群组链接：{bad[0]}（格式：https://www.facebook.com/groups/<名称>）"
            elif len(value) > MAX_GROUPS:
                errors[key] = f"最多 {MAX_GROUPS} 个群组"
            elif len({group_slug(v) for v in value}) < len(value):
                errors[key] = "两个链接指向同一个群组"
            else:
                clean[key] = value
        elif spec["type"] == "list":
            clean[key] = value
        elif "pattern" in spec and not spec["pattern"].match(value):
            errors[key] = "格式应为 https://www.facebook.com/groups/<名称>"
        else:
            clean[key] = value

    merged = {**current_settings(), **clean}
    if merged["MIN_DELAY"] > merged["MAX_DELAY"]:
        errors["MAX_DELAY"] = "最长暂停不能小于最短暂停"
    if "SELECTED_GROUP" in clean and clean["SELECTED_GROUP"] not in merged["GROUPS"]:
        errors["SELECTED_GROUP"] = "不在群组列表中"
    return clean, errors


def save_settings(clean):
    data = {**_read_settings_file(), **clean}
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def reset_settings():
    if os.path.isfile(SETTINGS_FILE):
        os.remove(SETTINGS_FILE)


def effective_settings():
    """Settings as a run uses them: switched-off features and low-risk mode applied."""
    s = current_settings()
    if not s["COLLECT_COMMENTS"]:
        s["MAX_COMMENTS_PER_POST"] = 0
        s["EXPAND_COMMENTS"] = False
    if not s["COLLECT_REACTIONS"]:
        s["EXPAND_REACTIONS"] = False
    s["MAX_DELAY"] = max(s["MIN_DELAY"], s["MAX_DELAY"])
    if s["LOW_RISK_MODE"]:
        s["MAX_POSTS"] = min(s["MAX_POSTS"], LOW_RISK_MODE_MAX_POSTS)
        s["BATCH_POSTS"] = min(s["BATCH_POSTS"], LOW_RISK_MODE_MAX_POSTS)
        s["MIN_DELAY"] = max(s["MIN_DELAY"], LOW_RISK_MODE_MIN_DELAY)
        s["MAX_DELAY"] = max(s["MAX_DELAY"], LOW_RISK_MODE_MAX_DELAY)
        s["EXPAND_COMMENTS"] = False
        s["EXPAND_REACTIONS"] = False
        s["UPDATE_SAVED_CLICKS"] = False
    if not s["UPDATE_SAVED"]:
        s["UPDATE_SAVED_CLICKS"] = False
    return s


_s = effective_settings()
GROUPS = _s["GROUPS"]
SELECTED_GROUP = _s["SELECTED_GROUP"]
SCRAPE_ALL_GROUPS = _s["SCRAPE_ALL_GROUPS"]
GROUP_PAUSE_MIN = _s["GROUP_PAUSE_MIN"]
BLOCK_MEDIA = _s["BLOCK_MEDIA"]
FAST_FORWARD = _s["FAST_FORWARD"]
SKIP_SEE_MORE_SAVED = _s["SKIP_SEE_MORE_SAVED"]
BATCH_MODE = _s["BATCH_MODE"]
BATCH_POSTS = _s["BATCH_POSTS"]
BATCH_TOTAL = _s["BATCH_TOTAL"]
BATCH_REST_MIN = _s["BATCH_REST_MIN"]
RECYCLE_MB = _s["RECYCLE_MB"]
SORT_CHRONOLOGICAL = _s["SORT_CHRONOLOGICAL"]
MAX_POSTS = _s["MAX_POSTS"]
SCROLL_LIMIT = _s["SCROLL_LIMIT"]
AUTO_SCROLL_LIMIT = _s["AUTO_SCROLL_LIMIT"]
# Hard ceiling for auto mode, so a run can never scroll forever
AUTO_SCROLL_CAP = SCHEMA["SCROLL_LIMIT"]["max"]
MAX_IDLE_SCROLLS = _s["MAX_IDLE_SCROLLS"]
MIN_DELAY = _s["MIN_DELAY"]
MAX_DELAY = _s["MAX_DELAY"]
MAX_COMMENTS_PER_POST = _s["MAX_COMMENTS_PER_POST"]
EXPAND_COMMENTS = _s["EXPAND_COMMENTS"]
COLLECT_REACTIONS = _s["COLLECT_REACTIONS"]
LOW_RISK_MODE = _s["LOW_RISK_MODE"]
UPDATE_SAVED = _s["UPDATE_SAVED"]
UPDATE_SAVED_CLICKS = _s["UPDATE_SAVED_CLICKS"]
EXPAND_REACTIONS = _s["EXPAND_REACTIONS"]
# Empty = Playwright's Chromium, falling back to an installed Chrome, then Edge
BROWSER_CHANNEL = _s["BROWSER_CHANNEL"].lower()
HEADLESS = _s["HEADLESS"]
ANONYMIZE = _s["ANONYMIZE"]

# Kept in .env only: it's a secret, and changing it breaks matching old hashes
HASH_SALT = os.getenv("HASH_SALT", "change-me")

# 127.0.0.1 = only this computer can open the dashboard. Docker needs 0.0.0.0.
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", 8765))
