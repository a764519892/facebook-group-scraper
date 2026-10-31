"""Persist which posts the user has already reviewed.

A mark is keyed by ``<group-slug>:<post-id>`` so the same Facebook post keeps
its state in the Posts, Opportunities and cross-group Leads lists. The file is
small JSON in ``data/reviewed.json``; writes are atomic so the dashboard never
reads a half-written file.
"""
import json
import os
import threading
from datetime import datetime

from config import REVIEWED_FILE

_LOCK = threading.Lock()
_MAX_KEY = 300


def review_key(group, post_id):
    """Stable key for one post, or "" when the post has no ID."""
    group = str(group or "").strip()
    post_id = str(post_id or "").strip()
    if not post_id:
        return ""
    return f"{group}:{post_id}" if group else post_id


def _read():
    try:
        with open(REVIEWED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    os.makedirs(os.path.dirname(REVIEWED_FILE), exist_ok=True)
    tmp = REVIEWED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REVIEWED_FILE)


def load():
    """All review marks: ``{key: reviewed_at}``."""
    with _LOCK:
        return _read()


def set_state(key, reviewed=True):
    """Mark or unmark one post. Returns the timestamp, or "" when unmarked."""
    key = str(key or "").strip()
    if not key:
        raise ValueError("缺少帖子标识")
    if len(key) > _MAX_KEY:
        raise ValueError("帖子标识太长")
    with _LOCK:
        data = _read()
        if reviewed:
            value = datetime.now().isoformat(timespec="seconds")
            data[key] = value
        else:
            value = ""
            data.pop(key, None)
        _write(data)
        return value


def annotate(posts, group=None):
    """Add ``review_key`` and ``reviewed_at`` to every post, in place."""
    marks = load()
    for post in posts:
        post_group = group or post.get("group") or ""
        key = review_key(post_group, post.get("post_id"))
        post["review_key"] = key
        post["reviewed_at"] = marks.get(key, "")
    return posts
