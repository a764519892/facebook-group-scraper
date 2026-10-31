import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

from config import (
    ANONYMIZE, BROWSER_CHANNEL, CHECKPOINT_FILE, DATA_DIR, GROUPS, HASH_SALT, LOG_FILE, RUN_LOCK_FILE,
    STATUS_FILE, GroupFiles,
)
from reactions import TYPES as REACTION_TYPES

# Windows consoles default to a legacy code page that can't print Vietnamese
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

FIELDS = ["post_id", "author", "time_text", "text", "post_url", "scraped_at"]
COMMENT_FIELDS = ["post_id", "comment_index", "author", "text", "scraped_at"]
# updated_at: when "update saved posts" last refreshed the row (empty = never)
# comment_count / share_count: the post's real totals from Facebook (empty = unknown)
REACTION_FIELDS = ["post_id", "total", *REACTION_TYPES, "comment_count", "share_count",
                   "source", "scraped_at", "updated_at"]

# A cell starting with one of these is executed as a formula by Excel/Sheets.
# Prefix it with an apostrophe when writing, and remove that apostrophe when
# reading so the dashboard still shows the original text.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_unescape(value):
    if isinstance(value, str) and len(value) > 1 and value[0] == "'" and value[1] in _FORMULA_PREFIXES:
        return value[1:]
    return value


def restrict_private_file(path):
    """Best effort: make a file readable only by its owner.

    POSIX has real mode bits. On Windows, inherited ACLs on some folders let
    every local user read the file, so remove inheritance and grant the current
    user full control. Failures are ignored: the file is still encrypted or
    contains only non-secret settings.
    """
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
            return
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if not user:
            return
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# Flags that cut memory without changing what pages see: no sound, no video
# autoplay, no background extras.
BROWSER_ARGS = [
    "--mute-audio",
    "--autoplay-policy=user-gesture-required",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
]


def launch_browser(playwright, headless):
    """Launch Playwright's Chromium, or fall back to an installed Chrome / Edge.

    Some PCs refuse to run the bundled Chromium (antivirus or app-control
    policies block unsigned browsers); real Chrome/Edge are signed and still work.
    """
    if BROWSER_CHANNEL:
        choices = [None if BROWSER_CHANNEL == "chromium" else BROWSER_CHANNEL]
    else:
        choices = [None, "chrome", "msedge"]

    errors = []
    for channel in choices:
        try:
            browser = playwright.chromium.launch(headless=headless, channel=channel, args=BROWSER_ARGS)
            print(f"Using browser: {channel or 'Playwright Chromium'} {browser.version}")
            return browser
        except Exception as e:
            errors.append(f"{channel or 'chromium'}: {str(e).splitlines()[0]}")
    raise RuntimeError("No browser could be started. Run install.py, or install Google Chrome.\n  "
                       + "\n  ".join(errors))


_BROWSER_PATHS = {
    "chrome": {
        "win32": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                  r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                  r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
        "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    },
    "msedge": {
        "win32": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                  r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
        "darwin": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "linux": ["microsoft-edge", "microsoft-edge-stable"],
    },
}


def find_browser_executable():
    """Path to an installed Chrome or Edge (BROWSER_CHANNEL picks which first)."""
    order = ["chrome", "msedge"]
    if BROWSER_CHANNEL in order:
        order.remove(BROWSER_CHANNEL)
        order.insert(0, BROWSER_CHANNEL)

    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    for name in order:
        for candidate in _BROWSER_PATHS[name].get(platform, []):
            path = os.path.expandvars(candidate) if os.sep in candidate else shutil.which(candidate)
            if path and os.path.isfile(path):
                return path
    raise RuntimeError("Couldn't find Google Chrome or Microsoft Edge. Install one of them, "
                       "or use: python login.py --playwright")


def log_message(message):
    line = f"{datetime.now()} - {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# Salts that come with the project or its examples: hashes made with them can
# be reversed by anyone who tries names from the group's member list
PLACEHOLDER_SALTS = {"", "change-me", "pick-any-random-string", "your-secret-salt"}


def privacy_warnings():
    """Reasons author names aren't properly protected (empty list = fine)."""
    if not ANONYMIZE:
        return ["作者姓名未哈希：会保存真实姓名。只有在对方同意时才关闭。"]
    if HASH_SALT.strip() in PLACEHOLDER_SALTS or len(HASH_SALT.strip()) < 8:
        return ["HASH_SALT 是占位值或太短，哈希后的姓名可能被猜出。请在收集数据前在 .env 中设置一个长随机值，并妥善保管。"]
    return []


def anonymize(name):
    if not name or not ANONYMIZE:
        return name
    return "user_" + hashlib.sha256((HASH_SALT + name).encode("utf-8")).hexdigest()[:12]


def load_existing_ids(files):
    return {row["post_id"] for row in read_rows(files.posts)}


def migrate_legacy_data():
    """Older versions kept one group's CSVs directly in data/. Move them into
    data/groups/<that group>/ the first time this version runs."""
    legacy = [n for n in ("posts.csv", "comments.csv", "reactions.csv",
                          "posts.csv.bak", "comments.csv.bak") if os.path.isfile(os.path.join(DATA_DIR, n))]
    if not legacy:
        return None
    target = GroupFiles(GROUPS[0])
    os.makedirs(target.dir, exist_ok=True)
    moved, kept = [], []
    for name in legacy:
        dest = os.path.join(target.dir, name)
        if os.path.exists(dest):
            kept.append(name)  # never overwrite; leave the old file where it is
            continue
        os.replace(os.path.join(DATA_DIR, name), dest)
        moved.append(name)
    return {"group": target.slug, "moved": moved, "kept": kept}


def describe_migration(result):
    """Log lines for migrate_legacy_data()'s result."""
    if not result:
        return []
    lines = []
    if result["moved"]:
        lines.append(f"Moved {', '.join(result['moved'])} from data/ into data/groups/{result['group']}/")
    if result["kept"]:
        lines.append(f"Left {', '.join(result['kept'])} in data/: data/groups/{result['group']}/ already has "
                     "files with those names. Merge them by hand if you need the old rows.")
    return lines


def write_status(status):
    """Write live progress for the dashboard (atomic, so it never reads half a file)."""
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    status["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False)
    os.replace(tmp, STATUS_FILE)


class RunLock:
    """Cross-process "only one scrape at a time" lock.

    Uses an OS file lock, so it is released automatically even if the process
    crashes. The file is in data/run.lock and is safe to leave behind.
    """

    def __init__(self, path=RUN_LOCK_FILE):
        self.path = path
        self.file = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            try:
                f = open(self.path, "r+b")
            except FileNotFoundError:
                f = open(self.path, "w+b")
        except OSError:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(f"pid={os.getpid()} at={datetime.now().isoformat(timespec='seconds')}\n".encode("utf-8"))
        f.flush()
        self.file = f
        return True

    def release(self):
        f, self.file = self.file, None
        if not f:
            return
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            f.close()

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Another scrape is already running")
        return self

    def __exit__(self, *exc):
        self.release()


def run_lock_is_held(path=RUN_LOCK_FILE):
    """True if another process currently holds the scrape lock."""
    lock = RunLock(path)
    if not lock.acquire():
        return True
    lock.release()
    return False


def backfill_post_urls(files):
    """Fill post_url for older CSV rows that predate the column."""
    if not os.path.isfile(files.posts):
        return 0
    rows = read_rows(files.posts)
    changed = 0
    for row in rows:
        if (row.get("post_url") or "").strip():
            continue
        pid = (row.get("post_id") or "").strip()
        if pid.isdigit() and (files.url or "").startswith("http"):
            row["post_url"] = files.url.rstrip("/") + "/posts/" + pid
            changed += 1
    if changed:
        _write_rows(files.posts, FIELDS, rows)
    return changed


def update_post_urls(files, updates):
    """Fill missing post_url values for posts that were saved earlier."""
    if not updates or not os.path.isfile(files.posts):
        return 0
    rows = read_rows(files.posts)
    changed = 0
    for row in rows:
        pid = row.get("post_id")
        if pid in updates and not (row.get("post_url") or "").strip():
            row["post_url"] = updates[pid]
            changed += 1
    if changed:
        _write_rows(files.posts, FIELDS, rows)
    return changed
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: csv_unescape(v) for k, v in row.items()} for row in csv.DictReader(f)]


def read_rows(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: csv_unescape(v) for k, v in row.items()} for row in csv.DictReader(f)]


def _write_rows(path, fields, rows):
    """Rewrite a whole CSV atomically."""
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([{k: csv_safe(v) for k, v in row.items()} for row in rows])
    os.replace(tmp, path)


def _ensure_header(path, fields):
    """Files from older versions may lack newer columns: add them (empty)."""
    if not os.path.isfile(path):
        return
    with open(path, newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), None)
    if not header:
        return
    if header != fields:
        _write_rows(path, fields, read_rows(path))


def _append_csv(path, fields, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _ensure_header(path, fields)
    # An existing but empty file still needs its header (older versions wrote
    # a data row into an empty file, which made read_csv/DictReader return []).
    file_exists = os.path.isfile(path) and os.path.getsize(path) > 0

    # utf-8-sig so Excel shows Vietnamese characters correctly
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerows([{k: csv_safe(v) for k, v in row.items()} for row in rows])


def record_checkpoint(message):
    """Remember that Facebook blocked or challenged us, for the dashboard warning."""
    data = {"at": datetime.now().isoformat(timespec="seconds"), "message": message, "acknowledged": False}
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def save_posts_to_csv(files, posts):
    _append_csv(files.posts, FIELDS, posts)


def save_comments_to_csv(files, comments):
    _append_csv(files.comments, COMMENT_FIELDS, comments)


def save_reactions_to_csv(files, rows):
    _append_csv(files.reactions, REACTION_FIELDS, rows)


def load_saved_engagement(files):
    """For updating saved posts: {post_id: reaction row} and {post_id: [comments]}."""
    reacts = {r["post_id"]: r for r in read_rows(files.reactions)}
    comments = {}
    for c in read_rows(files.comments):
        comments.setdefault(c["post_id"], []).append(c)
    return reacts, comments


_BUTTON_LINE = re.compile(r"^\s*(xem thêm|ẩn bớt|see more|see less)\s*$", re.I)
_TRAILING_LESS = re.compile(r"\s*(ẩn bớt|see less)\s*$", re.I)
_TRAILING_MORE = re.compile(r"(…|\.\.\.)\s*(xem thêm|see more)\s*$", re.I)


def clean_text(text):
    """Remove Facebook's "Xem thêm"/"Ẩn bớt" button labels (same rules as cleanText in scraper.py).

    A cut-off comment keeps its "…" so it's clear the text is incomplete.
    """
    lines = [l for l in (text or "").split("\n") if not _BUTTON_LINE.match(l)]
    text = _TRAILING_LESS.sub("", "\n".join(lines))
    return _TRAILING_MORE.sub("…", text).strip()


def clean_saved_files(files):
    """Fix text saved by older versions that kept the button labels. Backs up first.

    Returns {file name: rows fixed}. Safe to run any number of times.
    """
    fixed = {}
    for path, fields in ((files.posts, FIELDS), (files.comments, COMMENT_FIELDS)):
        rows = read_rows(path)
        changed = 0
        for r in rows:
            clean = clean_text(r.get("text", ""))
            if clean != r.get("text", ""):
                r["text"] = clean
                changed += 1
        if changed:
            backup = path + ".bak"
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
            _write_rows(path, fields, rows)
            fixed[f"{files.slug}/{os.path.basename(path)}"] = changed
    return fixed


def update_comment_texts(files, fixes):
    """Replace the text of saved comments: {(post_id, comment_index): full text}."""
    if not fixes:
        return
    rows = read_rows(files.comments)
    for r in rows:
        new = fixes.get((r["post_id"], str(r["comment_index"])))
        if new is not None:
            r["text"] = new
    _write_rows(files.comments, COMMENT_FIELDS, rows)


def update_reactions_csv(files, updated):
    """Replace the rows of posts in `updated` ({post_id: row}); add ones not in the file yet."""
    if not updated:
        return
    rows = read_rows(files.reactions)
    have = set()
    for i, r in enumerate(rows):
        if r["post_id"] in updated:
            rows[i] = updated[r["post_id"]]
            have.add(r["post_id"])
    rows += [r for pid, r in updated.items() if pid not in have]
    os.makedirs(files.dir, exist_ok=True)
    _write_rows(files.reactions, REACTION_FIELDS, rows)
