"""Who is this developer? A Tiger Analytics e-mail (and optionally a GitHub login) plus the token the usage API issued for this machine.

Stored per machine, outside any repo (a token must never be committed):
  $CCKIT_IDENTITY_FILE, else $XDG_CONFIG_HOME/cckit/identity.json, else %APPDATA%\\cckit\\identity.json, else ~/.config/cckit/identity.json
One file can hold several endpoints (one entry per usage API base URL).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_RX = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
EMAIL_RX = re.compile(r"^[a-z0-9][a-z0-9._%+\-]{0,63}@([a-z0-9.\-]{3,60})$")


class RegisterError(Exception):
    pass


def identity_path() -> Path:
    env = os.environ.get("CCKIT_IDENTITY_FILE")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "cckit" / "identity.json"


def _load() -> dict:
    p = identity_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_identity(endpoint: str) -> dict | None:
    rec = _load().get(endpoint)
    return rec if isinstance(rec, dict) and rec.get("email") and rec.get("token") else None


def save_identity(endpoint: str, email: str, github: str, token: str) -> Path:
    p = identity_path()
    data = _load()
    data[endpoint] = {"email": email, "github": github or "", "token": token}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # owner only: it holds a secret
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


# ----------------------------------------------------------------------------- the e-mail
def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def email_problem(email: str, domains: list[str]) -> str | None:
    """None when the address is acceptable, else a sentence for the developer."""
    e = normalise_email(email)
    m = EMAIL_RX.match(e)
    if not m or m.group(1) not in [d.lower() for d in domains]:
        names = " or ".join(f"@{d}" for d in domains)
        return f"'{email}' is not a {names} address. Use your company e-mail, for example firstname.lastname{('@' + domains[0]) if domains else ''}."
    return None


def detect_email(cwd: str | None, domains: list[str]) -> tuple[str, str] | None:
    """The developer's company e-mail from what they already set up: $CCKIT_EMAIL, then `git config user.email` (repo, then global).
    Only an address on an allowed domain is accepted, so a personal address in git config is ignored (never sent)."""
    cands = [("CCKIT_EMAIL", os.environ.get("CCKIT_EMAIL", "")), ("git config user.email", _run(["git", "config", "--get", "user.email"], cwd))]
    for src, v in cands:
        if v and email_problem(v, domains) is None:
            return normalise_email(v), src
    return None


# ----------------------------------------------------------------------------- the optional GitHub login
def valid_github(name: str) -> bool:
    return bool(GITHUB_RX.match(name or ""))


NOREPLY_RX = re.compile(r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$", re.I)
REMOTE_USER_RX = re.compile(r"^https?://([^/@:\s]+)(?::[^@/\s]*)?@github\.com[:/]", re.I)
NOT_A_USER = {"git", "x-access-token", "oauth2", "token", "gh", "github", "ssh"}      # logins that appear in URLs but are not people


def _run(cmd: list[str], cwd: str | None = None, timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def detect_github(cwd: str | None = None) -> tuple[str, str] | None:
    """Find the developer's GitHub login without asking (it is optional extra information). Returns (login, source) or None.

      1. $CCKIT_GITHUB                      explicit override
      2. git config github.user             (.git/config of the repo, else ~/.gitconfig)
      3. git config user.email              when it is GitHub's no-reply address (ID+login@users.noreply.github.com)
      4. `gh api user`                      the logged-in GitHub CLI account
      5. a remote URL with a user in it     https://login@github.com/org/repo  (tokens in the URL are never read)
      6. $GITHUB_USER / $GITHUB_ACTOR       Codespaces and CI
    `git config user.name` is NOT used: it is a display name ("Ada Lovelace"), not a login. The owner in a remote URL is the organisation.
    """
    env = os.environ
    if valid_github(env.get("CCKIT_GITHUB", "")):
        return env["CCKIT_GITHUB"], "CCKIT_GITHUB"
    v = _run(["git", "config", "--get", "github.user"], cwd)
    if valid_github(v):
        return v, "git config github.user"
    m = NOREPLY_RX.match(_run(["git", "config", "--get", "user.email"], cwd))
    if m and valid_github(m.group(1)):
        return m.group(1), "your git commit email (GitHub no-reply address)"
    v = _run(["gh", "api", "user", "--jq", ".login"], cwd)
    if valid_github(v):
        return v, "the GitHub CLI (gh)"
    for ln in _run(["git", "config", "--get-regexp", r"^remote\..*\.url$"], cwd).splitlines():
        m = REMOTE_USER_RX.match(ln.split(None, 1)[-1])
        if m and m.group(1).lower() not in NOT_A_USER and valid_github(m.group(1)):
            return m.group(1), "a git remote URL"
    for k in ("GITHUB_USER", "GITHUB_ACTOR"):
        if valid_github(env.get(k, "")):
            return env[k], k
    return None


# ----------------------------------------------------------------------------- talking to the APIs
def post_json(url: str, payload: dict, timeout: float = 20) -> dict:
    """POST JSON as text/plain (Apps Script reads the raw body). Apps Script answers with a redirect that urllib follows."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "text/plain;charset=utf-8", "User-Agent": "cckit"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise RegisterError(f"could not reach the usage endpoint: {getattr(e, 'reason', e)}") from e
    try:
        d = json.loads(body)
    except json.JSONDecodeError as e:
        raise RegisterError(SIGN_IN_HINT if looks_like_sign_in(body) else "the endpoint did not answer JSON. Is the URL a deployed /exec URL?") from e
    return d if isinstance(d, dict) else {}


SIGN_IN_HINT = ("the deployment asks for a Google sign-in, so command-line tools cannot use it. In Apps Script: Deploy > Manage deployments > "
                "edit the deployment > Who has access: Anyone (keep the Access Hub's own deployment restricted: use a second deployment for this).")


def looks_like_sign_in(body: str, final_url: str = "") -> bool:
    head = body.lstrip()[:200].lower()
    return "accounts.google.com" in final_url or "/servicelogin" in final_url.lower() or head.startswith("<!doctype html") or head.startswith("<html")


def ping(url: str, timeout: float = 15) -> dict:
    sep = "&" if "?" in url else "?"
    try:
        with urllib.request.urlopen(urllib.request.Request(url + sep + "api=ping", headers={"User-Agent": "cckit"}), timeout=timeout) as r:
            body, final = r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.URLError as e:
        raise RegisterError(f"ping failed: {getattr(e, 'reason', e)}") from e
    try:
        d = json.loads(body)
    except ValueError as e:
        raise RegisterError(SIGN_IN_HINT if looks_like_sign_in(body, final) else "ping failed: the endpoint did not answer JSON. Is the URL a deployed /exec URL?") from e
    return d if isinstance(d, dict) else {}


def register(endpoint: str, register_url: str, team_key: str, email: str, github: str | None, domains: list[str], timeout: float = 30) -> dict:
    """Register this e-mail (checked against the allowed domains first); on success the token is saved for this machine."""
    problem = email_problem(email, domains)
    if problem:
        raise RegisterError(problem)
    if github and not valid_github(github):
        github = None
    d = post_json(register_url, {"v": 1, "email": normalise_email(email), "github": github or "", "team_key": team_key}, timeout)
    if not d.get("ok") or not d.get("token"):
        raise RegisterError(str(d.get("error") or "registration failed"))
    save_identity(endpoint, d.get("email") or normalise_email(email), d.get("github") or github or "", d["token"])
    return d
