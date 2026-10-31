"""Local dashboard for watching the scraper. Run: python dashboard.py"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import html
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import analytics
import reviewed as review_store
from config import (
    CHECKPOINT_FILE, DASHBOARD_HOST, DASHBOARD_PORT, LOG_FILE, RUN_LOCK_FILE, SCHEMA, STATUS_FILE, STOP_FILE,
    GroupFiles, current_settings, reset_settings, save_settings, validate_settings,
)
from utils import (
    backfill_post_urls, clean_saved_files, describe_migration, migrate_legacy_data, privacy_warnings,
    run_lock_is_held,
)
import import_session
import login
from secret_store import (
    PASSPHRASE_ENV, VaultError, account_info, forget_credentials, get_credentials, get_passphrase, has_saved_session,
    has_session, is_unlocked, save_credentials, save_session, set_passphrase, unlock, vault_exists,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "dashboard.html")

scrape_proc = None
MAX_BODY = 1_000_000

# Login passphrase attempts, so a script on this computer cannot brute-force
# the vault through the dashboard. The CLI still has its own prompt.
_unlock_attempts = []
_unlock_lock = threading.Lock()


def allowed_dashboard_hosts():
    """Hosts the dashboard accepts. DNS rebinding sends another Host header."""
    hosts = {f"localhost:{DASHBOARD_PORT}", f"127.0.0.1:{DASHBOARD_PORT}",
             f"[::1]:{DASHBOARD_PORT}"}
    for item in (os.getenv("DASHBOARD_ALLOWED_HOSTS") or "").split(","):
        item = item.strip().lower()
        if item:
            hosts.add(item if ":" in item else f"{item}:{DASHBOARD_PORT}")
    return hosts


def host_allowed(host):
    return bool(host) and host.lower() in allowed_dashboard_hosts()


def query_int(query, key, default, hi):
    try:
        return max(0, min(hi, int(query.get(key, default))))
    except (TypeError, ValueError):
        return default


def query_flag(query, key):
    return str(query.get(key, "")).lower() in ("1", "true", "yes", "on")


def unlock_blocked():
    """At most 10 wrong passphrases per 15 minutes, per dashboard process."""
    now = time.monotonic()
    with _unlock_lock:
        _unlock_attempts[:] = [t for t in _unlock_attempts if now - t < 15 * 60]
        if len(_unlock_attempts) >= 10:
            return round(15 * 60 - (now - _unlock_attempts[0]))
    return 0


def note_unlock(ok):
    with _unlock_lock:
        if ok:
            _unlock_attempts.clear()
        else:
            _unlock_attempts.append(time.monotonic())


def log_tail(n=300):
    """Last n log lines without reading a multi-megabyte log into memory."""
    if not os.path.isfile(LOG_FILE):
        return []
    try:
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        if size > 256 * 1024 and lines:
            lines = lines[1:]  # first line is probably partial
        return lines[-n:]
    except OSError:
        return []


_counts_cache = {}
_counts_lock = threading.Lock()


def file_counts(files):
    """Row/reaction counts with an mtime+size cache; stats polls every 2 s."""
    paths = (files.posts, files.comments, files.reactions)
    try:
        stamp = tuple((os.path.getmtime(p), os.path.getsize(p)) if os.path.isfile(p) else None
                      for p in paths)
    except OSError:
        stamp = None
    key = (files.slug, stamp)
    with _counts_lock:
        if key in _counts_cache:
            return _counts_cache[key]
    reactions = analytics.read_csv(files.reactions)
    value = {
        "posts": len(analytics.read_csv(files.posts)),
        "comments": len(analytics.read_csv(files.comments)),
        "reactions": sum(int(r.get("total") or 0) for r in reactions),
    }
    with _counts_lock:
        _counts_cache[key] = value
        # Keep the cache small when groups are edited often
        if len(_counts_cache) > 64:
            _counts_cache.clear()
            _counts_cache[key] = value
    return value


def group_files(slug=None):
    """The group asked for by slug, else the selected one. Always one from the settings list."""
    settings = current_settings()
    for url in settings["GROUPS"]:
        if slug and GroupFiles(url).slug == slug:
            return GroupFiles(url)
    return GroupFiles(settings["SELECTED_GROUP"])


def groups_payload():
    settings = current_settings()
    groups = []
    for url in settings["GROUPS"]:
        files = GroupFiles(url)
        groups.append({"slug": files.slug, "url": files.url, **file_counts(files)})
    return {
        "selected": GroupFiles(settings["SELECTED_GROUP"]).slug,
        "scrape_all": settings["SCRAPE_ALL_GROUPS"],
        "groups": groups,
    }


def stats(slug=None):
    files = group_files(slug)
    status = {}
    if os.path.isfile(STATUS_FILE):
        with open(STATUS_FILE, encoding="utf-8") as f:
            status = json.load(f)

    settings = current_settings()
    counts = file_counts(files)
    unlocked = is_unlocked()
    return {
        "status": status,
        "process_running": scrape_proc is not None and scrape_proc.poll() is None,
        "has_session": has_session(),
        # None while the vault is locked: we can't read the session without the passphrase
        "has_saved_session": has_saved_session() if unlocked else None,
        "unlocked": unlocked,
        "login_state": login_job["state"],
        "group": files.slug,
        "total_posts": counts["posts"],
        "total_comments": counts["comments"],
        "total_reactions": counts["reactions"],
        "groups": groups_payload(),
        # The dashboard process outlives settings changes, so read them fresh
        "checkpoint_alert": settings["CHECKPOINT_ALERT"],
        "low_risk_mode": settings["LOW_RISK_MODE"],
        # Read fresh: settings.json may have changed since the dashboard started
        "privacy_warning": ("作者姓名未哈希：会保存真实姓名。" if not settings["ANONYMIZE"]
                            else " ".join(privacy_warnings())),
        "recycle_mb": settings["RECYCLE_MB"],
        "checkpoint": read_checkpoint(),
        "log": log_tail(8),
    }


def scrape_running_elsewhere():
    """status.json says a run is in progress and was updated in the last few minutes."""
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            s = json.load(f)
        recent = datetime.now() - datetime.fromisoformat(s.get("updated_at", "")) < timedelta(minutes=5)
        return s.get("state") in ("starting", "running", "resting") and recent
    except (OSError, ValueError, TypeError):
        return False


def start_scrape():
    global scrape_proc
    if scrape_proc is not None and scrape_proc.poll() is None:
        return False, "已经有抓取任务在运行"
    if login_job["state"] == "waiting":
        return False, "请先完成或取消 Account 里的登录"
    if not is_unlocked():
        return False, "密码库已锁定，请在 Account 页输入密码。"
    if not has_saved_session():
        return False, "还没有保存的 Facebook 登录，请先在 Account 页登录。"
    if run_lock_is_held(RUN_LOCK_FILE) or scrape_running_elsewhere():
        return False, "已有抓取任务在运行（来自面板或终端）。"
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    # Hand the passphrase to the child through its environment, never a file
    env = dict(os.environ, **{PASSPHRASE_ENV: get_passphrase()})
    scrape_proc = subprocess.Popen([sys.executable, os.path.join(HERE, "main.py")], cwd=HERE, env=env)
    return True, "已开始抓取"


# ---- Account: logging in again from the dashboard ----------------------------

login_job = {"state": "idle", "message": "", "started_at": ""}
_login_stop = threading.Event()


def account_payload():
    info = {"vault_exists": vault_exists(), "unlocked": is_unlocked(), "login": dict(login_job)}
    if is_unlocked() and vault_exists():
        try:
            info.update(account_info())
        except VaultError as e:
            info["error"] = str(e)
    return info


def use_passphrase(body, rate=True):
    """The vault needs a passphrase before anything can be saved in it."""
    if is_unlocked():
        return None
    if rate:
        wait = unlock_blocked()
        if wait:
            return f"尝试次数太多，请在 {wait} 秒后再试。"
    passphrase = body.get("passphrase") or ""
    if not vault_exists() and passphrase != (body.get("passphrase_confirm") or ""):
        if rate:
            note_unlock(False)
        return "两次输入的密码不一致。"
    try:
        set_passphrase(passphrase)
    except VaultError as e:
        if rate:
            note_unlock(False)
        return str(e)
    if rate:
        note_unlock(True)
    return None


def save_cookies(body):
    error = use_passphrase(body)
    if error:
        return False, error
    try:
        cookies = (import_session.parse_text(body["text"]) if (body.get("text") or "").strip()
                   else import_session.from_fields(body.get("fields") or {}))
        session = import_session.session_from_cookies(cookies)
        save_session(session)
    except (ValueError, KeyError) as e:
        return False, f"未保存：{e}"
    except VaultError as e:
        return False, str(e)
    return True, f"已保存 {len(session['cookies'])} 个 Facebook Cookie，可以开始抓取。"


def start_browser_login(body):
    if login_job["state"] == "waiting":
        return False, "登录窗口已经打开"
    if scrape_proc is not None and scrape_proc.poll() is None:
        return False, "请等抓取结束"
    error = use_passphrase(body)
    if error:
        return False, error

    email, password = (body.get("email") or "").strip(), body.get("password") or ""
    if body.get("use_saved"):
        try:
            email, password = get_credentials()
        except VaultError as e:
            return False, str(e)
        if not (email and password):
            return False, "密码库里没有保存的邮箱和密码"
    if body.get("remember") and email and password and not body.get("use_saved"):
        try:
            save_credentials(email, password)
        except VaultError as e:
            return False, str(e)
    autofill = (email, password) if email and password else None

    def status(message):
        login_job["message"] = message

    def run():
        try:
            session = login.browser_login(autofill=autofill, should_stop=_login_stop.is_set, on_status=status)
            if session:
                save_session(session)
                login_job.update(state="done", message="登录成功并已保存，可以开始抓取。")
            else:
                login_job.update(state="idle", message=login_job["message"] or "登录已取消。")
        except Exception as e:  # window closed, no browser found, vault problem...
            login_job.update(state="error", message=str(e))

    _login_stop.clear()
    login_job.update(state="waiting", message="正在打开登录窗口…",
                     started_at=datetime.now().isoformat(timespec="seconds"))
    threading.Thread(target=run, daemon=True).start()
    return True, "正在打开登录窗口"


def stop_scrape():
    """Ask the running scrape to stop: it finishes the current scroll, saves, and exits."""
    if scrape_proc is None or scrape_proc.poll() is not None:
        if not scrape_running_elsewhere():
            return False, "当前没有抓取任务"
    with open(STOP_FILE, "w") as f:
        f.write("stop")
    return True, "正在停止：完成当前滚动后保存并关闭…"


def read_checkpoint():
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def acknowledge_checkpoint():
    data = read_checkpoint()
    if data:
        data["acknowledged"] = True
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


def settings_payload():
    schema = {
        key: {k: v for k, v in spec.items() if k != "pattern"}
        for key, spec in SCHEMA.items()
    }
    return {"schema": schema, "values": current_settings()}


def outreach_payload():
    """Preview what the auto-reply would send, plus what has been sent today."""
    import outreach
    settings = current_settings()
    state = outreach.load_state()
    today = outreach.today_count(state)
    sent = list(state.get("commented", {}).items())
    sent.sort(key=lambda kv: kv[1].get("at", ""), reverse=True)
    try:
        found = outreach.candidates(settings) if settings.get("AUTO_REPLY") else []
    except Exception as e:
        return {"enabled": bool(settings.get("AUTO_REPLY")), "error": str(e),
                "today": today, "limit": settings.get("AUTO_REPLY_DAILY_LIMIT"),
                "pending": [], "sent": sent[:20]}
    pending = [{
        "group": p.get("group"), "post_id": p.get("post_id"), "url": p.get("url"),
        "text": p.get("text"), "product": (p.get("product") or {}).get("name"),
        "reply": p.get("reply"),
    } for p in found]
    return {
        "enabled": bool(settings.get("AUTO_REPLY")),
        "today": today,
        "limit": settings.get("AUTO_REPLY_DAILY_LIMIT"),
        "min_minutes": settings.get("AUTO_REPLY_MIN_MINUTES"),
        "max_minutes": settings.get("AUTO_REPLY_MAX_MINUTES"),
        "pending": pending,
        "sent": [{"key": k, **v} for k, v in sent[:20]],
    }


_TRANSLATION_CACHE = {}


def _translate_text(text, target="zh-CN"):
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > 4000:
        text = text[:4000] + "…"
    key = (target, text)
    if key in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[key]
    errors = []
    # Google Translate (unofficial, no key). Handles mixed Vietnamese/English.
    try:
        url = ("https://translate.googleapis.com/translate_a/single"
               "?client=gtx&sl=auto&tl=" + urllib.parse.quote(target)
               + "&dt=t&q=" + urllib.parse.quote(text))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        translated = "".join(part[0] for part in data[0]
                            if isinstance(part, list) and part and isinstance(part[0], str))
        translated = html.unescape(translated).strip()
        if translated:
            _TRANSLATION_CACHE[key] = translated
            return translated
    except Exception as e:
        errors.append(f"Google: {e}")
    # MyMemory fallback.
    try:
        url = ("https://api.mymemory.translated.net/get?q=" + urllib.parse.quote(text)
               + "&langpair=vi|" + urllib.parse.quote(target))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        translated = html.unescape(data.get("responseData", {}).get("translatedText", "")).strip()
        if translated and "PLEASE SELECT TWO DISTINCT LANGUAGES" not in translated.upper():
            _TRANSLATION_CACHE[key] = translated
            return translated
    except Exception as e:
        errors.append(f"MyMemory: {e}")
    raise RuntimeError("翻译服务暂时不可用：" + "；".join(errors))


class Handler(BaseHTTPRequestHandler):
    def host_ok(self):
        return host_allowed(self.headers.get("Host", ""))

    def same_origin_json(self):
        """Block other websites from driving the dashboard (CSRF/DNS rebinding).

        The Host allowlist rejects DNS rebinding even when the attacker also
        sends a matching Origin. Cross-origin JSON still needs a CORS preflight,
        which this server never approves.
        """
        if not self.host_ok():
            return False
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in ("http", "https") and host_allowed(parsed.netloc)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length < 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_json(self, data, code=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, body, filename):
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.host_ok():
            return self.send_json({"ok": False, "message": "未知的访问地址"}, 403)
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            with open(PAGE, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")  # always the latest page after an update
            self.security_headers()
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/stats":
            self.send_json(stats(query.get("group")))
        elif url.path == "/api/posts":
            self.send_json(analytics.list_posts(
                group_files(query.get("group")), query.get("q", "").strip(), query.get("sort", "newest"),
                query_int(query, "offset", 0, 10**6), query_int(query, "limit", 20, 100),
                reviewed=review_store.load(), hide_reviewed=query_flag(query, "hide_reviewed")))
        elif url.path == "/api/analytics":
            self.send_json(analytics.analytics(group_files(query.get("group"))))
        elif url.path == "/api/leads":
            settings = current_settings()
            report = analytics.leads(
                settings,
                days=query_int(query, "days", settings["OPPORTUNITY_DAYS"], 3650),
                limit=query_int(query, "limit", 100, 500),
                sort=query.get("sort", "score"),
                reviewed=review_store.load(),
                hide_reviewed=query_flag(query, "hide_reviewed"),
            )
            if query.get("format") == "csv":
                data = analytics.opportunities_csv(report).encode("utf-8")
                return self.send_download(data, "high-intent-leads.csv")
            self.send_json(report)
        elif url.path == "/api/outreach":
            self.send_json(outreach_payload())
        elif url.path == "/api/translate":
            text = query.get("text", "")
            target = query.get("target", "zh-CN")
            if not text.strip():
                return self.send_json({"ok": False, "message": "没有可翻译的文本"}, 400)
            if len(text) > 8000:
                text = text[:8000]
            try:
                translated = _translate_text(text, target)
                self.send_json({"ok": True, "original": text, "translated": translated,
                                "target": target})
            except Exception as e:
                self.send_json({"ok": False, "message": str(e)}, 502)
        elif url.path == "/api/opportunities":
            files = group_files(query.get("group"))
            settings = current_settings()
            report = analytics.opportunities(
                files, settings,
                days=query_int(query, "days", settings["OPPORTUNITY_DAYS"], 3650),
                query=query.get("q", "").strip(),
                kind=query.get("kind", "all"),
                limit=query_int(query, "limit", 100, 500),
                sort=query.get("sort", "score"),
                reviewed=review_store.load(),
                hide_reviewed=query_flag(query, "hide_reviewed"),
            )
            if query.get("format") == "csv":
                data = analytics.opportunities_csv(report).encode("utf-8")
                return self.send_download(data, f"opportunities-{files.slug}.csv")
            self.send_json(report)
        elif url.path == "/api/account":
            self.send_json(account_payload())
        elif url.path == "/api/log":
            self.send_json({"lines": log_tail(1000)})
        elif url.path == "/api/settings":
            self.send_json(settings_payload())
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return self.send_json({"ok": False, "message": "请求内容太大"}, 413)
        if not self.same_origin_json():
            return self.send_json({"ok": False, "message": "已拒绝"}, 403)
        if self.path == "/api/settings":
            body = self.read_json()
            if body.get("reset"):
                reset_settings()
                return self.send_json({"ok": True, "message": "已恢复 .env 默认值", **settings_payload()})
            clean, errors = validate_settings(body.get("values", {}))
            if errors:
                return self.send_json({"ok": False, "errors": errors, "message": "请修改标红的字段"}, 400)
            save_settings(clean)
            running = scrape_proc is not None and scrape_proc.poll() is None
            message = "已保存，将在下次抓取生效。" if running else "已保存。"
            self.send_json({"ok": True, "message": message, **settings_payload()})
        elif self.path == "/api/reviewed":
            body = self.read_json()
            key = str(body.get("key") or "").strip()
            reviewed = body.get("reviewed", True)
            try:
                reviewed_at = review_store.set_state(key, bool(reviewed))
            except ValueError as e:
                return self.send_json({"ok": False, "message": str(e)}, 400)
            except OSError as e:
                return self.send_json({"ok": False, "message": f"保存失败：{e}"}, 500)
            self.send_json({"ok": True, "key": key, "reviewed": bool(reviewed_at),
                            "reviewed_at": reviewed_at})
        elif self.path == "/api/checkpoint/ack":
            acknowledge_checkpoint()
            self.send_json({"ok": True})
        elif self.path == "/api/stop":
            ok, message = stop_scrape()
            self.send_json({"ok": ok, "message": message}, 200 if ok else 409)
        elif self.path == "/api/run":
            ok, message = start_scrape()
            self.send_json({"ok": ok, "message": message}, 200 if ok else 409)
        elif self.path.startswith("/api/account/"):
            body = self.read_json()
            action = self.path.rsplit("/", 1)[-1]
            if action == "unlock":
                wait = unlock_blocked()
                if wait:
                    return self.send_json({"ok": False, "message": f"尝试次数太多，请在 {wait} 秒后再试。"},
                                          429, {"Retry-After": str(wait)})
                error = use_passphrase(body, rate=False)
                note_unlock(not error)
                ok, message = (False, error) if error else (True, "已解锁")
            elif action == "cookies":
                ok, message = save_cookies(body)
            elif action == "login":
                ok, message = start_browser_login(body)
            elif action == "cancel":
                _login_stop.set()
                ok, message = True, "正在取消…"
            elif action == "forget-password":
                if not is_unlocked():
                    return self.send_json({"ok": False, "message": "请先解锁密码库"}, 401)
                try:
                    forget_credentials()
                    ok, message = True, "已删除保存的邮箱和密码"
                except VaultError as e:
                    ok, message = False, str(e)
            else:
                return self.send_error(404)
            # Never echo anything back from the request: it may hold secrets
            self.send_json({"ok": ok, "message": message, **account_payload()}, 200 if ok else 400)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # keep the terminal quiet; the page polls every few seconds


def main(open_browser=True, prompt=True):
    os.chdir(HERE)
    # A scrape started elsewhere may be writing the CSVs; it cleans them itself then
    if not scrape_running_elsewhere() and not run_lock_is_held(RUN_LOCK_FILE):
        for line in describe_migration(migrate_legacy_data()):
            print(line)
        for url in current_settings()["GROUPS"]:
            for name, n in clean_saved_files(GroupFiles(url)).items():
                print(f"Removed 'Xem thêm'/'Ẩn bớt' button text from {n} rows of {name} (backup: {name}.bak)")
            n = backfill_post_urls(GroupFiles(url))
            if n:
                print(f"Filled post_url for {n} saved posts in {GroupFiles(url).slug}")
    if has_session() and prompt:
        try:
            unlock()
        except VaultError as e:
            # Keep going: the passphrase can be entered in the Account tab
            print(f"{e} 密码库保持锁定，直到你在 Account 页解锁。")
    else:
        print("还没有保存的登录：请在面板的 Account 页登录。")

    url = f"http://localhost:{DASHBOARD_PORT}" + ("" if has_session() else "/#account")
    try:
        server = ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), Handler)
    except OSError:
        sys.exit(f"端口 {DASHBOARD_PORT} 已被占用：面板可能已经在 {url} 打开。"
                 "请关闭另一个面板，或在 .env 里设置 DASHBOARD_PORT。")
    print(f"面板运行于 {url}  （按 Ctrl+C 停止）")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dashboard stopped.")


if __name__ == "__main__":
    # --no-prompt: don't ask for the passphrase in the terminal (unlock in the Account tab)
    main(open_browser="--no-browser" not in sys.argv, prompt="--no-prompt" not in sys.argv)
