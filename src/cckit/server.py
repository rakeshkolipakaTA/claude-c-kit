"""The register and usage APIs as a small standalone HTTP service (standard library only, data in SQLite).

Why this exists: a Google Apps Script web app can only be called without a Google sign-in when it is deployed with access "Anyone",
and many organisations forbid that. This server offers the SAME two APIs from any host you control (a VM, a container, a PaaS), and
the Google Sheet stays the reporting view: an Apps Script time trigger (Pull.gs) pulls new rows from `?api=export`, which only needs
outbound access.

  POST ?api=register   {"email", "github"?, "team_key"}                       -> {"ok", "email", "github", "token", "devices", "verified"}
  POST ?api=usage      {"email", "token", "event_id", "trigger", "model", ...} -> {"ok"}
  GET  ?api=ping                                                              -> {"ok", "service", "ready"}
  GET  ?api=export&table=events|users&after=ID&limit=N   (Authorization: Bearer <admin token>)   -> rows for the sheet

Same rules as the Apps Script version: a company e-mail on an allowed domain, the team key to register, a per-machine token to report
(only SHA-256 hashes are stored, up to 5 machines per developer), length-limited values with spreadsheet formulas neutralised,
hourly limits, repeated events dropped. Nothing from the conversation is ever stored.

Run behind HTTPS (a reverse proxy or the platform's load balancer): the client refuses plain http except for localhost.
Configuration is by environment variable so that secrets stay out of the process list:
  CCKIT_TEAM_KEY (required)   CCKIT_ADMIN_TOKEN (needed for ?api=export)   CCKIT_ALLOWED_DOMAINS (default tigeranalytics.com, comma separated)
  CCKIT_DB (default usage.db)   PORT / --port   CCKIT_HOST / --host
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_BODY = 6000
HARD_BODY_LIMIT = 1 << 16
MAX_DEVICES = 5
EVENTS_PER_HOUR = 120          # per developer
REGISTER_PER_HOUR = 30         # for everybody together
EMAIL_RX = re.compile(r"^[a-z0-9][a-z0-9._%+\-]{0,63}@([a-z0-9.\-]{3,60})$")
GITHUB_RX = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
CONTROL_RX = re.compile(r"[\x00-\x1f\x7f]")

# (sheet header, column) : the order is the order of the sheet. `trig` and `win` avoid SQL keywords.
EVENT_COLS = [
    ("Received At", "received_at"), ("Event ID", "event_id"), ("Email", "email"), ("GitHub", "github"), ("Trigger", "trig"),
    ("Model", "model"), ("Effort", "effort"), ("Claude Version", "claude_version"), ("Entrypoint", "entrypoint"),
    ("Context Tokens", "context_tokens"), ("Window", "win"), ("Context %", "context_pct"), ("Input Tokens", "input_tokens"),
    ("Output Tokens", "output_tokens"), ("Cache Read", "cache_read"), ("Cache Write", "cache_write"), ("Cache Hit %", "cache_hit_pct"),
    ("Requests", "requests"), ("Subagent Requests", "sub_requests"), ("Subagent Output", "sub_output"), ("Tool Calls", "tool_calls"),
    ("Models", "models"), ("Tools", "tools"), ("Session Minutes", "session_minutes"), ("Compactions Before", "compactions_before"),
    ("Project", "project"), ("Branch", "branch"), ("Session", "session"), ("OS", "os"), ("Partial", "partial"), ("Client", "client"),
]
USER_COLS = [("Email", "email"), ("GitHub", "github"), ("Registered At", "registered_at"), ("GitHub Verified", "github_verified"),
             ("Devices", "devices"), ("Active", "active"), ("Last Seen", "last_seen"), ("Compactions", "compactions"), ("Note", "note")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  email TEXT PRIMARY KEY, github TEXT NOT NULL DEFAULT '', registered_at TEXT NOT NULL, github_verified TEXT NOT NULL DEFAULT '',
  token_hashes TEXT NOT NULL DEFAULT '', active TEXT NOT NULL DEFAULT 'Yes', last_seen TEXT NOT NULL DEFAULT '',
  compactions INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, email TEXT NOT NULL, github TEXT NOT NULL,
  trig TEXT NOT NULL, model TEXT, effort TEXT, claude_version TEXT, entrypoint TEXT, context_tokens INTEGER, win INTEGER, context_pct REAL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER, cache_hit_pct REAL, requests INTEGER,
  sub_requests INTEGER, sub_output INTEGER, tool_calls INTEGER, models TEXT, tools TEXT, session_minutes INTEGER,
  compactions_before INTEGER, project TEXT, branch TEXT, session TEXT, os TEXT, partial TEXT, client TEXT);
CREATE TABLE IF NOT EXISTS hits(name TEXT NOT NULL, hour INTEGER NOT NULL, n INTEGER NOT NULL, PRIMARY KEY(name, hour));
"""


class ApiError(Exception):
    """An error the caller should see (everything else becomes a generic 'server error')."""


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def clean_str(v, limit: int) -> str:
    """Text for a cell: limited length, no control characters, and never starts like a spreadsheet formula."""
    s = CONTROL_RX.sub(" ", "" if v is None else str(v)).strip()[:limit]
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def clean_num(v, limit: float = 1e12) -> int:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0
    if isinstance(v, bool) or n != n or n < 0 or n in (float("inf"),):
        return 0
    return int(min(n + 0.5, limit))


def pct(a: int, b: int):
    return int(a * 1000 / b + 0.5) / 10 if b > 0 else None


def github_exists(login: str) -> str:
    """'Yes' when GitHub knows the account, 'No' when it does not, 'Unchecked' when GitHub could not be asked."""
    req = urllib.request.Request("https://api.github.com/users/" + login, headers={"Accept": "application/vnd.github+json", "User-Agent": "cckit-usage-api"})
    try:
        with urllib.request.urlopen(req, timeout=5):
            return "Yes"
    except urllib.error.HTTPError as e:
        return "No" if e.code == 404 else "Unchecked"
    except (urllib.error.URLError, OSError, ValueError):
        return "Unchecked"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UsageApi:
    """The two APIs plus export, independent of HTTP so that they can be tested and reused."""

    def __init__(self, db_path: str, team_key: str, domains=("tigeranalytics.com",), admin_token: str = "", github_check=github_exists,
                 max_devices: int = MAX_DEVICES, events_per_hour: int = EVENTS_PER_HOUR, register_per_hour: int = REGISTER_PER_HOUR, clock=time.time):
        if not team_key:
            raise ValueError("a team key is required (CCKIT_TEAM_KEY)")
        self.db_path, self.team_key, self.admin_token = db_path, team_key, admin_token
        self.domains = [d.strip().lower() for d in domains if d.strip()]
        self.github_check, self.max_devices, self.clock = github_check, max_devices, clock
        self.events_per_hour, self.register_per_hour = events_per_hour, register_per_hour
        self.lock = threading.Lock()
        with self._db() as c:
            c.executescript(SCHEMA)

    # ---------------------------------------------------------------- plumbing
    @contextmanager
    def _db(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def _hit(self, c, name: str, limit: int) -> bool:
        hour = int(self.clock() // 3600)
        c.execute("DELETE FROM hits WHERE hour < ?", (hour - 1,))
        c.execute("INSERT INTO hits(name, hour, n) VALUES(?, ?, 1) ON CONFLICT(name, hour) DO UPDATE SET n = n + 1", (name, hour))
        n = c.execute("SELECT n FROM hits WHERE name = ? AND hour = ?", (name, hour)).fetchone()["n"]
        c.commit()                                          # a refused request still counts against the limit
        return n <= limit

    def _email(self, v) -> str:
        e = str(v or "").strip().lower()
        m = EMAIL_RX.match(e)
        if not m or m.group(1) not in self.domains:
            raise ApiError("email must be a " + " or ".join("@" + d for d in self.domains) + " address")
        return e

    # ---------------------------------------------------------------- the two APIs
    def register(self, b: dict) -> dict:
        email = self._email(b.get("email"))
        if not hmac.compare_digest(str(b.get("team_key") or ""), self.team_key):
            raise ApiError("wrong team key")
        gh = str(b.get("github") or "").strip()
        gh = gh if GITHUB_RX.match(gh) else ""
        with self.lock, self._db() as c:
            if not self._hit(c, "reg", self.register_per_hour):
                raise ApiError("too many registrations this hour, try later")
            row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            hashes = [h for h in (row["token_hashes"] if row else "").split(",") if h]
            if row:
                if row["active"].lower() == "no":
                    raise ApiError("this user is switched off by an admin")
                if len(hashes) >= self.max_devices:
                    raise ApiError(f"already registered on {len(hashes)} machines. Ask an admin to reset {email} (cckit server reset {email}), then register again.")
            verified = self.github_check(gh) if gh else ""
            token = secrets.token_hex(32)
            hashes.append(sha256(token))
            if row:
                if gh:
                    c.execute("UPDATE users SET github = ?, github_verified = ? WHERE email = ?", (gh, verified, email))
                c.execute("UPDATE users SET token_hashes = ? WHERE email = ?", (",".join(hashes), email))
            else:
                c.execute("INSERT INTO users(email, github, registered_at, github_verified, token_hashes) VALUES(?,?,?,?,?)",
                          (email, gh, now_iso(), verified, ",".join(hashes)))
            return {"ok": True, "email": email, "github": gh or (row["github"] if row else ""), "token": token, "devices": len(hashes),
                    "verified": verified == "Yes"}

    def usage(self, b: dict) -> dict:
        email = self._email(b.get("email"))
        with self.lock, self._db() as c:
            row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                raise ApiError("unknown user: register first")
            if sha256(str(b.get("token") or "")) not in row["token_hashes"].split(","):
                raise ApiError("bad token")
            if row["active"].lower() == "no":
                raise ApiError("this user is switched off by an admin")
            if not self._hit(c, "ev:" + email, self.events_per_hour):
                raise ApiError("rate limit")
            eid = clean_str(b.get("event_id"), 64)
            if not eid:
                raise ApiError("event_id is required")
            if c.execute("SELECT 1 FROM events WHERE event_id = ?", (eid,)).fetchone():
                return {"ok": True, "duplicate": True}
            n = lambda k, lim=1e12: clean_num(b.get(k), lim)
            ctx, win = n("context_tokens", 5e6), n("window", 5e6)
            inp, out, cr, cw = n("input_tokens"), n("output_tokens"), n("cache_read"), n("cache_write")
            s = lambda k, lim: clean_str(b.get(k), lim)
            c.execute(
                "INSERT INTO events(received_at, event_id, email, github, trig, model, effort, claude_version, entrypoint, context_tokens, win, context_pct,"
                " input_tokens, output_tokens, cache_read, cache_write, cache_hit_pct, requests, sub_requests, sub_output, tool_calls, models, tools,"
                " session_minutes, compactions_before, project, branch, session, os, partial, client) VALUES(" + ",".join("?" * 31) + ")",
                (now_iso(), eid, email, row["github"], "manual" if b.get("trigger") == "manual" else "auto", s("model", 60), s("effort", 20),
                 s("claude_version", 20), s("entrypoint", 30), ctx, win, pct(ctx, win), inp, out, cr, cw, pct(cr, inp + cr + cw),
                 n("requests"), n("sub_requests"), n("sub_output"), n("tool_calls"), s("models", 300), s("tools", 300),
                 n("session_minutes", 1e6), n("compactions_before", 1e5), s("project", 80), s("branch", 80), s("session", 64), s("os", 20),
                 "Yes" if b.get("partial") else "", s("client", 40)))
            c.execute("UPDATE users SET last_seen = ?, compactions = compactions + 1 WHERE email = ?", (now_iso(), email))
            return {"ok": True}

    # ---------------------------------------------------------------- export (for the sheet) and admin
    def export(self, table: str, after: int = 0, limit: int = 500) -> dict:
        """Rows with the sheet's headers. Events are paged by id (`last_id` is the cursor); users are always complete. No token hashes."""
        limit = max(1, min(int(limit), 2000))
        with self._db() as c:
            if table == "events":
                cols = [k for _, k in EVENT_COLS]
                rows = c.execute(f"SELECT id, {', '.join(cols)} FROM events WHERE id > ? ORDER BY id LIMIT ?", (int(after), limit)).fetchall()
                return {"ok": True, "columns": [h for h, _ in EVENT_COLS], "rows": [["" if r[k] is None else r[k] for k in cols] for r in rows],
                        "last_id": rows[-1]["id"] if rows else int(after), "more": len(rows) == limit}
            if table == "users":
                rows = c.execute("SELECT *, (CASE WHEN token_hashes = '' THEN 0 ELSE length(token_hashes) - length(replace(token_hashes, ',', '')) + 1 END) AS devices"
                                 " FROM users ORDER BY registered_at, email").fetchall()
                return {"ok": True, "columns": [h for h, _ in USER_COLS], "rows": [["" if r[k] is None else r[k] for _, k in USER_COLS] for r in rows]}
        raise ApiError("table must be events or users")

    def set_active(self, email: str, active: bool) -> bool:
        with self.lock, self._db() as c:
            return c.execute("UPDATE users SET active = ? WHERE email = ?", ("Yes" if active else "No", email.strip().lower())).rowcount > 0

    def reset(self, email: str) -> bool:
        """Forget every machine's token: the developer registers again at the next session."""
        with self.lock, self._db() as c:
            return c.execute("UPDATE users SET token_hashes = '' WHERE email = ?", (email.strip().lower(),)).rowcount > 0

    # ---------------------------------------------------------------- HTTP-shaped entry point
    def handle(self, method: str, query: dict, body: bytes, auth: str = "") -> tuple[int, dict]:
        """Returns (http status, JSON object). Application errors are 200 with ok:false, like the Apps Script version."""
        api = (query.get("api") or [""])[0]
        try:
            if method == "GET":
                if api == "ping":
                    return 200, {"ok": True, "service": "cckit-usage", "ready": True}
                if api == "export":
                    if not self.admin_token:
                        return 403, {"ok": False, "error": "export is not enabled (set CCKIT_ADMIN_TOKEN)"}
                    if not hmac.compare_digest(auth, "Bearer " + self.admin_token):
                        return 401, {"ok": False, "error": "bad admin token"}
                    q = lambda k, d: (query.get(k) or [d])[0]
                    return 200, self.export(q("table", "events"), int(q("after", "0") or 0), int(q("limit", "500") or 500))
                return 200, {"ok": True, "service": "cckit-usage"}
            if method == "POST":
                if len(body) > MAX_BODY:
                    raise ApiError("payload too large")
                try:
                    data = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    raise ApiError("body must be JSON") from None
                if not isinstance(data, dict):
                    raise ApiError("body must be a JSON object")
                if api == "register":
                    return 200, self.register(data)
                if api == "usage":
                    return 200, self.usage(data)
                raise ApiError("unknown api: use ?api=register or ?api=usage")
            return 405, {"ok": False, "error": "method not allowed"}
        except ApiError as e:
            return 200, {"ok": False, "error": str(e)}
        except (ValueError, TypeError):
            return 400, {"ok": False, "error": "bad request"}
        except Exception:                                      # never leak internals to an anonymous caller
            return 500, {"ok": False, "error": "server error"}


def make_server(host: str, port: int, api: UsageApi) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "cckit-usage"
        sys_version = ""

        def _send(self, status: int, obj: dict) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _go(self, method: str) -> None:
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self._send(200, {"ok": True})
            body = b""
            if method == "POST":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > HARD_BODY_LIMIT:                  # refuse without reading: do not let a stranger make us buffer megabytes
                    self.close_connection = True
                    return self._send(200, {"ok": False, "error": "payload too large"})
                body = self.rfile.read(n)                           # up to 64 KB, so the client always gets its answer
            self._send(*api.handle(method, parse_qs(u.query), body, self.headers.get("Authorization", "")))

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, fmt, *args):                    # no request bodies, no query strings (tokens never reach a log)
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv
