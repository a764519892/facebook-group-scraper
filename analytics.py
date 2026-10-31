"""Reads the CSVs and computes what the dashboard's Posts, Analytics and
Opportunities tabs show."""
import csv
import io
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from urllib.parse import quote

from config import GroupFiles, SCHEMA
from reactions import TYPES as REACTION_TYPES
from reviewed import review_key
from utils import csv_safe, csv_unescape

# Common Vietnamese/English filler syllables, skipped when counting phrases
STOPWORDS = set("""
và của có cho mình là các những được không thì này đó với ở một em anh chị ạ ơi nha nhé
ai gì mọi người cái để trong đã sẽ khi cũng như bạn ko k hay hoặc nào thế vậy rồi nữa
lại ra vào từ về theo nên mà nếu đang bị còn đi làm tại do rất lắm thôi luôn đây kia
ad admin mn mng mọi ib inbox cmt the a an of to and or is are in on for you i it this
""".split())
# Function words/pronouns that make a raw Chinese n-gram look like noise.
# Product terms such as 进口线 and price questions such as 多少钱 do not use these.
CJK_STOP_CHARS = set("我你他她它们这那想买要卖求谁没在是了着过就都也很还和与或把被给让对从到为而且但所以如果因为啊吗呢吧哦呀嘛有出")


def read_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: csv_unescape(v) for k, v in row.items()} for row in csv.DictReader(f)]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def real_comments(p):
    """Facebook's comment count if known, else how many we saved."""
    return p["comment_count"] if p["comment_count"] is not None else len(p["comments"])


def load(files):
    """Posts joined with their comments and reaction counts, newest scrape first."""
    posts = read_csv(files.posts)
    comments = defaultdict(list)
    for c in read_csv(files.comments):
        comments[c.get("post_id")].append(c)
    reactions = {r.get("post_id"): r for r in read_csv(files.reactions)}

    joined = []
    for p in posts:
        pid = p.get("post_id")
        r = reactions.get(pid)
        counts = {t: int((r or {}).get(t) or 0) for t in REACTION_TYPES}
        posted_at, has_time = parse_post_time(p.get("time_text", ""), p.get("scraped_at", ""))
        joined.append({
            **p,
            "comments": sorted(comments.get(pid, []), key=lambda c: int(c.get("comment_index") or 0)),
            "reactions": counts,
            "reaction_total": int((r or {}).get("total") or 0),
            "reaction_source": (r or {}).get("source", ""),
            # Facebook's real totals; None = not read (older data, or the line wasn't there)
            "comment_count": _int_or_none((r or {}).get("comment_count")),
            "share_count": _int_or_none((r or {}).get("share_count")),
            "reaction_updated_at": (r or {}).get("updated_at", ""),
            "posted_at": posted_at.isoformat(timespec="minutes") if posted_at else "",
            "posted_has_time": has_time,
        })
    joined.reverse()
    return joined


# --- Post time ---------------------------------------------------------------
# Facebook shows relative times ("2 giờ", "3d", "Hôm qua lúc 14:30") or dates
# ("12 tháng 10", "October 12 at 2:30 PM"). Turned into a datetime using the
# moment the post was scraped. Best effort: unknown formats give None.

_REL = [
    (re.compile(r"^(\d+)\s*(phút|p|m|min|mins|minutes?)\b", re.I), "minutes"),
    (re.compile(r"^(\d+)\s*(giờ|g|h|hr|hrs|hours?)\b", re.I), "hours"),
    (re.compile(r"^(\d+)\s*(ngày|d|days?)\b", re.I), "days"),
    (re.compile(r"^(\d+)\s*(tuần|w|weeks?)\b", re.I), "weeks"),
    # Do not treat "12 tháng 10" (12 October) as "12 months ago"
    (re.compile(r"^(\d+)\s*(tháng|thg|months?|mos?)\b(?!\s*\d)", re.I), "months"),
    (re.compile(r"^(\d+)\s*(năm|years?|yrs?|y)\b", re.I), "years"),
]
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
               "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
    "thứ hai": 0, "thứ 2": 0, "thứ ba": 1, "thứ 3": 1, "thứ tư": 2, "thứ 4": 2,
    "thứ năm": 3, "thứ 5": 3, "thứ sáu": 4, "thứ 6": 4, "thứ bảy": 5, "thứ 7": 5,
    "chủ nhật": 6, "chúa nhật": 6, "cn": 6,
}
_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _WEEKDAYS), key=len, reverse=True)) + r")\b", re.I)
_CLOCK = re.compile(r"(?:(?:lúc|at)\s*)?(\d{1,2}):(\d{2})\s*(am|pm|sa|ch)?", re.I)


def _clock(text):
    m = _CLOCK.search(text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if hour > 23 or minute > 59:
        return None
    if ampm in ("pm", "ch") and hour < 12:
        hour += 12
    if ampm in ("am", "sa") and hour == 12:
        hour = 0
    return hour, minute


def parse_post_time(text, scraped_at):
    """Return (datetime or None, whether the time of day is known)."""
    try:
        base = datetime.fromisoformat(scraped_at)
    except (TypeError, ValueError):
        return None, False
    text = (text or "").strip()
    low = text.lower()
    if not text:
        return None, False
    if low.startswith(("vừa xong", "just now")):
        return base, True
    for pattern, unit in _REL:
        m = pattern.match(low)
        if m:
            n = int(m.group(1))
            if unit == "months":
                when = base - timedelta(days=30 * n)
            elif unit == "years":
                when = base - timedelta(days=365 * n)
            else:
                when = base - timedelta(**{unit: n})
            return when, unit in ("minutes", "hours")
    clock = _clock(low)
    if low.startswith(("hôm kia", "day before yesterday")):
        day = base - timedelta(days=2)
        if clock:
            return day.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0), True
        return day, False
    if low.startswith(("hôm qua", "yesterday")):
        day = base - timedelta(days=1)
        if clock:
            return day.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0), True
        return day, False
    if low.startswith(("hôm nay", "today")):
        if clock:
            return base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0), True
        return base, False

    # "Thứ Hai lúc 14:30" / "Monday at 2:30 PM": most recent occurrence.
    m = _WEEKDAY_RE.search(low)
    if m:
        target = _WEEKDAYS[m.group(1).lower()]
        delta = (base.weekday() - target) % 7
        when = base - timedelta(days=delta)
        if clock:
            when = when.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            if when > base:  # this weekday's time hasn't happened yet -> last week
                when -= timedelta(days=7)
            return when, True
        return when.replace(hour=0, minute=0, second=0, microsecond=0), False

    year = base.year
    m = re.search(r"(\d{1,2})\s*tháng\s*(\d{1,2})(?:,?\s*(\d{4}))?", low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else year
    else:
        month_names = "|".join(list(_MONTHS) + list(_MONTH_ABBR))
        m = re.search(r"(" + month_names + r")\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?", low)
        if m:
            month = _MONTHS.get(m.group(1), _MONTH_ABBR.get(m.group(1)))
            day = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else year
        else:
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", low)
            if not m:
                return None, False
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        when = datetime(year, month, day, *(clock or (0, 0)))
    except ValueError:
        return None, False
    if when > base + timedelta(days=1):  # "12 tháng 12" seen in January means last year
        try:
            when = when.replace(year=when.year - 1)
        except ValueError:
            return None, False
    return when, bool(clock)


# --- Posts tab ---------------------------------------------------------------

SORTS = {
    "newest": lambda p: p.get("scraped_at", ""),
    "reactions": lambda p: p["reaction_total"],
    "comments": real_comments,
    "shares": lambda p: p["share_count"] or 0,
}


def list_posts(files, query="", sort="newest", offset=0, limit=20, reviewed=None, hide_reviewed=False):
    posts = load(files)
    if reviewed is not None:
        for p in posts:
            key = review_key(files.slug, p.get("post_id"))
            p["review_key"] = key
            p["reviewed_at"] = reviewed.get(key, "")
    if hide_reviewed:
        posts = [p for p in posts if not p.get("reviewed_at")]
    if query:
        q = query.lower()
        posts = [p for p in posts if q in p.get("text", "").lower()
                 or any(q in c.get("text", "").lower() for c in p["comments"])]
    if sort in SORTS and sort != "newest":
        posts.sort(key=SORTS[sort], reverse=True)
    for p in posts:
        if not p.get("url"):
            pid = p.get("post_id")
            if pid and str(pid).isdigit() and (files.url or "").startswith("http"):
                p["url"] = files.url.rstrip("/") + "/posts/" + str(pid)
            else:
                p["url"] = p.get("post_url", "") or ""
    return {"total": len(posts), "posts": posts[offset:offset + limit]}


# --- Analytics tab -----------------------------------------------------------

def top_phrases(posts, n=12):
    # Vietnamese words are mostly two syllables, so count syllable pairs.
    # Chinese has no spaces, so also count 2-4 character CJK terms and keep
    # the longest term when a shorter one is only a piece of it.
    counts = Counter()
    for post in posts:
        text = re.sub(r"https?://\S+", " ", post.get("text", "").lower())[:3000]
        syllables = [w for w in re.findall(r"[^\W\d_]+", text)
                     if not re.search(r"[\u4e00-\u9fff]", w)]
        pairs = {
            f"{a} {b}" for a, b in zip(syllables, syllables[1:])
            if a not in STOPWORDS and b not in STOPWORDS
        }
        counts.update(pairs)  # once per post, so one spammy post can't dominate
        terms = set()
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            for size in (4, 3, 2):
                for i in range(len(run) - size + 1):
                    term = run[i:i + size]
                    if not any(ch in CJK_STOP_CHARS for ch in term) and not any(ch in STOPWORDS for ch in term):
                        terms.add(term)
        counts.update(terms)  # once per post, even if the term repeats
    # Drop 2-char pieces such as "进口" when "进口线" is just as common.
    ranked = counts.most_common(max(n * 4, 40))
    out = []
    for term, count in ranked:
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            longer = [c for t, c in ranked if len(t) > len(term) and t != term and term in t]
            if longer and max(longer) >= count:
                continue
        out.append((term, count))
        if len(out) >= n:
            break
    return out


COMMENT_BUCKETS = [("0", 0, 0), ("1-5", 1, 5), ("6-20", 6, 20), ("21-50", 21, 50), ("51+", 51, None)]
REACTION_BUCKETS = [("0", 0, 0), ("1-5", 1, 5), ("6-20", 6, 20), ("21-50", 21, 50),
                    ("51-100", 51, 100), ("101-500", 101, 500), ("500+", 501, None)]


def group_summary(files):
    """Row counts for the group picker, without parsing everything."""
    reactions = read_csv(files.reactions)
    return {
        "slug": files.slug, "url": files.url,
        "posts": len(read_csv(files.posts)),
        "comments": len(read_csv(files.comments)),
        "reactions": sum(int(r.get("total") or 0) for r in reactions),
    }


def analytics(files):
    posts = load(files)
    n = len(posts)
    total_reactions = sum(p["reaction_total"] for p in posts)
    total_comments = sum(len(p["comments"]) for p in posts)

    by_type = {t: sum(p["reactions"][t] for p in posts) for t in REACTION_TYPES}

    buckets = []
    for label, lo, hi in REACTION_BUCKETS:
        buckets.append([label, sum(1 for p in posts
                                   if p["reaction_total"] >= lo and (hi is None or p["reaction_total"] <= hi))])

    counted = [p for p in posts if p["comment_count"] is not None]
    shared = [p for p in posts if p["share_count"] is not None]
    total_real_comments = sum(p["comment_count"] for p in counted)
    total_shares = sum(p["share_count"] for p in shared)
    comment_buckets = [[label, sum(1 for p in counted if p["comment_count"] >= lo
                                   and (hi is None or p["comment_count"] <= hi))]
                       for label, lo, hi in COMMENT_BUCKETS]

    per_day = Counter(p["posted_at"][:10] for p in posts if p["posted_at"])
    days = sorted(per_day)
    if days:
        start, end = datetime.fromisoformat(days[0]), datetime.fromisoformat(days[-1])
        start = max(start, end - timedelta(days=59))  # last 60 days at most
        series = []
        d = start
        while d <= end:
            key = d.date().isoformat()
            series.append([key, per_day.get(key, 0)])
            d += timedelta(days=1)
    else:
        series = []

    # Weekday x hour, only for posts whose time of day is known
    heat = [[0] * 24 for _ in range(7)]
    timed = 0
    for p in posts:
        if p["posted_at"] and p["posted_has_time"]:
            when = datetime.fromisoformat(p["posted_at"])
            heat[when.weekday()][when.hour] += 1
            timed += 1

    top = sorted(posts, key=lambda p: p["reaction_total"], reverse=True)[:10]
    return {
        "posts": n,
        "comments": total_comments,
        "reactions": total_reactions,
        "avg_reactions": round(total_reactions / n, 1) if n else 0,
        "with_comments_pct": round(100 * sum(1 for p in posts if real_comments(p)) / n) if n else 0,
        "real_comments": total_real_comments,
        "avg_comments": round(total_real_comments / len(counted), 1) if counted else 0,
        "shares": total_shares,
        "counted_posts": len(counted),
        "dated_posts": sum(1 for p in posts if p["posted_at"]),
        "reactions_by_type": by_type,
        "reaction_sources": dict(Counter(p["reaction_source"] or "none" for p in posts)),
        "reaction_buckets": buckets,
        "comment_distribution": comment_buckets,
        "posts_per_day": series,
        "heatmap": heat,
        "heatmap_posts": timed,
        "top_posts": [{"text": p.get("text", "")[:140], "time_text": p.get("time_text", ""),
                       "total": p["reaction_total"], "reactions": p["reactions"],
                       "comments": real_comments(p), "shares": p["share_count"] or 0}
                      for p in top if p["reaction_total"]],
        "top_phrases": top_phrases(posts),
        "unique_authors": len({p.get("author") for p in posts if p.get("author")}),
    }


# --- Opportunities tab: demand, hot posts and discussion --------------------

def _keyword_hits(text, keywords):
    """Which configured words occur in text. ASCII words use word boundaries;
    Chinese/Vietnamese terms are matched as substrings."""
    low = (text or "").lower()
    hits = []
    for raw in keywords or []:
        word = str(raw).strip()
        if not word:
            continue
        low_word = word.lower()
        if re.search(r"[a-z0-9]", low_word):
            if re.search(r"(?<![a-z0-9])" + re.escape(low_word) + r"(?![a-z0-9])", low):
                hits.append(word)
        elif low_word in low:
            hits.append(word)
    return hits


def _percentile(values, fraction):
    values = sorted(v for v in values if v is not None)
    if not values:
        return 0
    pos = (len(values) - 1) * fraction
    lo, hi = int(pos), min(len(values) - 1, int(pos) + 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


_QUANTITY_RE = re.compile(
    r"(?<![\w])\d{1,4}\s*(?:c|cuộn|cối|sét|bó|lá|con|chiếc)\b|\b(?:sl|sll)\b|\bsố lượng\b",
    re.I,
)


def _setting(settings, key):
    """A caller's value when present, otherwise the schema default."""
    if key in settings:
        return settings[key]
    return SCHEMA[key]["default"]


def _search_text(p):
    """A distinctive phrase for Facebook's in-group search."""
    candidates = [p.get("text") or ""] + [c.get("text") or "" for c in p.get("comments", [])]
    cleaned = []
    for raw in candidates:
        lines = []
        for line in re.split(r"[\r\n]+", raw):
            line = re.sub(r"\s+", " ", line).strip()
            if not line or line in ("Tác giả", "Theo dõi"):
                continue
            if re.fullmatch(r"\d+\s*(phút|giờ|ngày|tuần|tháng|năm|ph|h|d|w|m|y)", line, re.I):
                continue
            lines.append(line)
        text = " ".join(lines).strip()
        if len(text) >= 12:
            cleaned.append(text)
    return max(cleaned, key=len)[:120] if cleaned else ""


def _group_search_url(files, text):
    if not text or not (files.url or "").startswith("http"):
        return ""
    return files.url.rstrip("/") + "/search/?q=" + quote(text, safe="")


def _post_url(files, post_id, stored=None):
    if stored:
        return stored
    if not post_id or not str(post_id).isdigit() or not (files.url or "").startswith("http"):
        return ""
    return files.url.rstrip("/") + "/posts/" + str(post_id)


def opportunities(files, settings, days=None, query="", kind="all", limit=100, sort="score",
                  reviewed=None, hide_reviewed=False):
    """Posts that are worth acting on: recent demand, hot posts, discussions.

    A post joins the result when it falls in the look-back window and matches
    the requested filter. Scoring is deliberately simple and explainable:
    engagement + demand/focus keyword hits + a recency bonus.
    """
    days = max(1, min(3650, int(days or settings.get("OPPORTUNITY_DAYS", 30))))
    sort = sort if sort in ("score", "reactions", "comments", "shares", "engagement") else "score"
    focus = _setting(settings, "FOCUS_KEYWORDS")
    demand_words = _setting(settings, "DEMAND_KEYWORDS")
    strong_demand = _setting(settings, "STRONG_DEMAND_KEYWORDS")
    seller_blockers = _setting(settings, "SELLER_BLOCKERS")
    supply_words = _setting(settings, "SUPPLY_KEYWORDS")
    ignore = _setting(settings, "IGNORE_KEYWORDS")
    demand_requires_focus = bool(_setting(settings, "DEMAND_REQUIRES_FOCUS"))
    now = datetime.now()
    cutoff = now - timedelta(days=days)

    rows = []
    for p in load(files):
        post_text = p.get("text", "") or ""
        comment_text = "\n".join(c.get("text", "") for c in p["comments"])
        haystack = post_text + "\n" + comment_text
        if _keyword_hits(haystack, ignore):
            continue
        try:
            when = datetime.fromisoformat(p.get("posted_at") or p.get("scraped_at") or "")
        except (TypeError, ValueError):
            continue
        if when < cutoff:
            continue
        approx = not p.get("posted_at")
        age = max(0.0, (now - when).total_seconds() / 86400)
        age = round(age, 1)
        focus_hits = _keyword_hits(haystack, focus)
        demand_hits = _keyword_hits(haystack, demand_words)
        strong_hits = _keyword_hits(haystack, strong_demand)
        # Seller words in the post itself suppress generic "cần/mua" demand.
        # A strong buyer phrase ("cần mua", "shop nào bán"...) wins over them.
        seller_hits = _keyword_hits(post_text, seller_blockers)
        demand_ok = bool(demand_hits or strong_hits) and (not demand_requires_focus or bool(focus_hits))
        if demand_ok and seller_hits and not strong_hits:
            demand_ok = False
        supply_hits = _keyword_hits(haystack, supply_words)
        high_intent = demand_ok and (bool(strong_hits) or bool(_QUANTITY_RE.search(haystack)))
        comments = real_comments(p)
        shares = p.get("share_count") or 0
        engagement = p["reaction_total"] + 2 * comments + 3 * shares
        rows.append({
            **p,
            "_when": when,
            "_haystack": haystack,
            "group": files.slug,
            "group_url": files.url,
            "url": _post_url(files, p.get("post_id"), p.get("post_url")),
            "search_url": _group_search_url(files, _search_text(p)),
            "age_days": age,
            "date_approx": approx,
            "comments_saved": len(p["comments"]),
            "reaction_count": p["reaction_total"],
            "engagement": engagement,
            "focus_hits": focus_hits,
            "demand_hits": demand_hits,
            "strong_demand_hits": strong_hits,
            "seller_hits": seller_hits,
            "demand_ok": demand_ok,
            "high_intent": high_intent,
            "supply_hits": supply_hits,
        })


    if reviewed is not None:
        for r in rows:
            key = review_key(files.slug, r.get("post_id"))
            r["review_key"] = key
            r["reviewed_at"] = reviewed.get(key, "")
    if hide_reviewed:
        rows = [r for r in rows if not r.get("reviewed_at")]

    if not rows:
        return {
            "summary": {
                "days": days, "posts": 0, "demand": 0, "supply": 0, "hot": 0,
                "leads": 0, "discussed": 0, "fresh": 0, "dated_posts": 0, "undated_posts": 0,
                "hot_threshold": 0, "discussion_threshold": 0, "focus_counts": [],
                "demand_focus_counts": [], "demand_counts": [], "top_topics": [], "daily": [],
            },
            "total": 0, "posts": [],
        }

    hot_threshold = max(1, _percentile([r["reaction_count"] for r in rows], 0.75))
    discussion_threshold = max(1, _percentile([real_comments(r) for r in rows], 0.75))
    for r in rows:
        r["hot"] = r["reaction_count"] >= hot_threshold
        r["discussed"] = real_comments(r) >= discussion_threshold
        r["fresh"] = r["age_days"] <= 7
        r["supply_only"] = bool(r["supply_hits"]) and not r["demand_ok"]
        r["demand_blocked"] = bool(r["demand_hits"]) and not r["demand_ok"]
        tags = []
        if r["high_intent"]:
            tags.append("高意向")
        if r["demand_ok"]:
            tags.append("需求")
        if r["supply_only"]:
            tags.append("供应")
        if r["demand_blocked"]:
            tags.append("卖家帖")
        if r["hot"]:
            tags.append("热帖")
        if r["discussed"]:
            tags.append("讨论多")
        if r["fresh"]:
            tags.append("新")
        if r["date_approx"]:
            tags.append("日期近似")
        r["tags"] = tags
        recency = max(0.0, 1 - r["age_days"] / max(days, 1))
        r["score"] = round(
            r["engagement"]
            + 30 * bool(r["demand_ok"])
            + 25 * bool(r["high_intent"])
            + (12 * len(r["demand_hits"]) if r["demand_ok"] else 0)
            + 15 * len(r["focus_hits"])
            + 10 * bool(r["supply_only"])
            + 20 * recency,
            1,
        )

    def selected(r):
        if kind == "lead" and not r["high_intent"]:
            return False
        if kind == "demand" and not r["demand_ok"]:
            return False
        if kind == "supply" and not r["supply_only"]:
            return False
        if kind == "hot" and not r["hot"]:
            return False
        if kind == "discussed" and not r["discussed"]:
            return False
        if kind == "fresh" and not r["fresh"]:
            return False
        return True

    q = (query or "").strip().lower()
    matched = [r for r in rows if selected(r) and (not q or q in r["_haystack"].lower())]
    if sort == "score":
        matched.sort(key=lambda r: (r["high_intent"], r["score"], r["_when"]), reverse=True)
    else:
        metric = {
            "reactions": lambda r: r["reaction_count"],
            "comments": real_comments,
            "shares": lambda r: r["share_count"] or 0,
            "engagement": lambda r: r["engagement"],
        }[sort]
        matched.sort(key=lambda r: (metric(r), r["high_intent"], r["_when"]), reverse=True)

    total = len(matched)
    matched = matched[:max(1, min(500, int(limit or 100)))]

    focus_counts = Counter()
    demand_focus_counts = Counter()
    demand_counts = Counter()
    for r in rows:
        focus_counts.update(set(r["focus_hits"]))
        if r["demand_ok"]:
            demand_counts.update(set(r["demand_hits"]))
            demand_focus_counts.update(set(r["focus_hits"]))

    daily = []
    span = min(days, 30)
    today = now.date()
    by_day = defaultdict(lambda: {"posts": 0, "demand": 0, "hot": 0})
    for r in rows:
        if (today - r["_when"].date()).days < span:
            key = r["_when"].date().isoformat()
            by_day[key]["posts"] += 1
            by_day[key]["demand"] += bool(r["demand_ok"])
            by_day[key]["hot"] += bool(r["hot"])

    for i in range(span - 1, -1, -1):
        key = (today - timedelta(days=i)).isoformat()
        daily.append({"date": key, **by_day.get(key, {"posts": 0, "demand": 0, "hot": 0})})

    for r in matched:
        r.pop("_when", None)
        r.pop("_haystack", None)
        # Keep the JSON small: each post already carries its comments separately.
        r.pop("reactions", None)
        r.pop("reaction_source", None)
        r.pop("reaction_updated_at", None)
        r.pop("posted_has_time", None)

    return {
        "summary": {
            "days": days,
            "posts": len(rows),
            "demand": sum(1 for r in rows if r["demand_ok"]),
            "leads": sum(1 for r in rows if r["high_intent"]),
            "supply": sum(1 for r in rows if r["supply_only"]),
            "hot": sum(1 for r in rows if r["hot"]),
            "discussed": sum(1 for r in rows if r["discussed"]),
            "fresh": sum(1 for r in rows if r["fresh"]),
            "dated_posts": sum(1 for r in rows if not r["date_approx"]),
            "undated_posts": sum(1 for r in rows if r["date_approx"]),
            "hot_threshold": round(hot_threshold, 1),
            "discussion_threshold": round(discussion_threshold, 1),
            "focus_counts": focus_counts.most_common(12),
            "demand_focus_counts": demand_focus_counts.most_common(12),
            "demand_counts": demand_counts.most_common(12),
            "top_topics": top_phrases(rows, 12),
            "daily": daily,
        },
        "total": total,
        "posts": matched,
    }


def leads(settings, days=30, limit=100, sort="score", reviewed=None, hide_reviewed=False):
    """High-intent buyer posts across every configured group."""
    days = max(1, min(3650, int(days or 30)))
    sort = sort if sort in ("score", "reactions", "comments", "shares", "engagement") else "score"
    posts = []
    for url in settings.get("GROUPS", []):
        report = opportunities(GroupFiles(url), settings, days=days, kind="lead", limit=500, sort=sort,
                               reviewed=reviewed, hide_reviewed=hide_reviewed)
        posts.extend(report["posts"])
    if sort == "score":
        posts.sort(key=lambda p: (p.get("score", 0), p.get("posted_at") or p.get("scraped_at") or ""),
                   reverse=True)
    else:
        metric = {
            "reactions": lambda p: p.get("reaction_count", 0),
            "comments": lambda p: real_comments(p),
            "shares": lambda p: p.get("share_count") or 0,
            "engagement": lambda p: p.get("engagement", 0),
        }[sort]
        posts.sort(key=lambda p: (metric(p), p.get("score", 0)), reverse=True)
    total = len(posts)
    return {
        "summary": {"days": days, "leads": total, "groups": len(settings.get("GROUPS", []))},
        "total": total,
        "posts": posts[:max(1, min(500, int(limit or 100)))],
    }


def opportunities_csv(report):
    """Excel-friendly CSV of the Opportunities tab (UTF-8 BOM + formula-safe)."""
    fields = ["group", "post_id", "author", "time_text", "posted_at", "date_approx", "text",
              "reactions", "comment_count", "comments_saved", "shares", "engagement", "score",
              "high_intent", "tags", "matched_focus", "matched_demand", "matched_strong",
              "matched_supply", "seller_hits", "url", "search_url", "reviewed_at", "comments"]
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for p in report.get("posts", []):
        row = {k: p.get(k, "") for k in fields}
        row["reactions"] = p.get("reaction_count", 0)
        row["shares"] = p.get("share_count") if p.get("share_count") is not None else ""
        row["comments"] = " | ".join(
            f"{c.get('author', '')}: {c.get('text', '')}" for c in p.get("comments", []))
        row["high_intent"] = "yes" if p.get("high_intent") else "no"
        row["tags"] = " ".join(p.get("tags", []))
        row["matched_focus"] = " | ".join(p.get("focus_hits", []))
        row["matched_demand"] = " | ".join(p.get("demand_hits", []))
        row["matched_strong"] = " | ".join(p.get("strong_demand_hits", []))
        row["matched_supply"] = " | ".join(p.get("supply_hits", []))
        row["seller_hits"] = " | ".join(p.get("seller_hits", []))
        writer.writerow({k: csv_safe(v) for k, v in row.items()})
    return "\ufeff" + out.getvalue()
