"""Usage reporting: config, generated hook, the two APIs (register, usage), Tiger e-mail checks, the standalone app, and end-to-end runs.

The end-to-end tests run the real usage app (cckit.server) over HTTP on a local port.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from cckit import apply, cli, config, doctor, identity, render, server as usage_server

KEY = "teamkey123"
ADMIN = "admintoken456"
DOM = "tigeranalytics.com"
ADA = "ada.lovelace@tigeranalytics.com"
GIT = shutil.which("git")


def read(p):
    return Path(p).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def ident(tmp_path, monkeypatch):
    """Identity file, git config and PATH are all private to the test, so the real machine's git/gh login and e-mail are never picked up."""
    monkeypatch.setenv("CCKIT_IDENTITY_FILE", str(tmp_path / "home" / "identity.json"))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    if GIT:
        (bin_ / "git").symlink_to(GIT)
    monkeypatch.setenv("PATH", str(bin_))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("CCKIT_GITHUB", "CCKIT_EMAIL", "GITHUB_USER", "GITHUB_ACTOR", "CCKIT_TELEMETRY", "CCKIT_TEAM_KEY"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "home" / "identity.json"


def fake_gh(tmp_path, login):
    gh = tmp_path / "bin" / "gh"
    gh.write_text(f"#!/bin/sh\necho {login}\n")
    gh.chmod(0o755)


def git(repo, *args):
    subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True)


def git_repo(path, email=None, gh_user=None):
    Path(path).mkdir(exist_ok=True)
    git(path, "init")
    if email:
        git(path, "config", "user.email", email)
    if gh_user:
        git(path, "config", "github.user", gh_user)
    return path


def write_cfg(repo, endpoint="http://127.0.0.1:9/exec", extra=""):
    (repo / "cckit.toml").write_text(f'[telemetry]\nenabled = true\nendpoint = "{endpoint}"\nteam_key = "{KEY}"\n{extra}')


# ------------------------------------------------------------------ config and generated files
def test_defaults_point_at_the_live_app_but_ship_no_team_key(tmp_path):
    cfg = config.defaults()["telemetry"]
    assert cfg["enabled"] is True and cfg["endpoint"] == ""
    assert cfg["register_url"] == "https://claude-code-usage.hatchable.site/api/register" and cfg["usage_url"] == "https://claude-code-usage.hatchable.site/api/usage"
    assert cfg["team_key"] == ""                                                    # a join code is never shipped inside a public package
    assert config.api_url(cfg, "ping") == "https://claude-code-usage.hatchable.site/api/ping"
    apply.init(tmp_path)
    s = json.loads(read(tmp_path / ".claude/settings.json"))
    assert "PreCompact" in s["hooks"] and "SessionStart" not in s["hooks"]          # nothing to register with yet: the start-up hook is not installed
    assert all("team" not in read(p).lower() or "team_key" not in read(p) or 'TEAM_KEY = ""' in read(p) for p in (tmp_path / ".claude/hooks").glob("*.py"))


def test_telemetry_switched_off_writes_no_reporting_hooks(tmp_path):
    (tmp_path / "cckit.toml").write_text("[telemetry]\nenabled = false\n")
    apply.init(tmp_path)
    s = json.loads(read(tmp_path / ".claude/settings.json"))
    assert "PreCompact" not in s["hooks"] and "SessionStart" not in s["hooks"]
    assert not (tmp_path / ".claude/hooks/compact_report.py").exists()


def test_a_users_own_endpoint_wins_over_the_shipped_default_urls(tmp_path):
    (tmp_path / "cckit.toml").write_text('[telemetry]\nendpoint = "https://mine.example.com/"\n')
    t = config.load(tmp_path)["telemetry"]
    assert (t["register_url"], t["usage_url"], t["ping_url"]) == ("", "", "")
    assert config.api_url(t, "register") == "https://mine.example.com/?api=register"
    (tmp_path / "cckit.toml").write_text('[telemetry]\nendpoint = "https://mine.example.com/"\nusage_url = "https://other.example.com/u"\n')
    t = config.load(tmp_path)["telemetry"]
    assert t["usage_url"] == "https://other.example.com/u" and config.api_url(t, "register") == "https://mine.example.com/?api=register"


def test_enabled_with_every_url_cleared_warns_and_writes_no_hook(tmp_path):
    (tmp_path / "cckit.toml").write_text('[telemetry]\nenabled = true\nregister_url = ""\nusage_url = ""\n')
    apply.init(tmp_path)
    assert "PreCompact" not in json.loads(read(tmp_path / ".claude/settings.json"))["hooks"]
    rep = doctor.run(tmp_path, check_claude=False)
    assert any(lv == "warn" and "no URL is set" in t for lv, t in rep.items)


@pytest.mark.parametrize("key", ["endpoint", "register_url", "usage_url"])
@pytest.mark.parametrize("url", ["http://example.com/exec", "ftp://x/y", "not a url", "https://"])
def test_bad_urls_rejected(key, url):
    cfg = config.defaults()
    cfg["telemetry"][key] = url
    assert any(key in e for e in config.validate(cfg))


@pytest.mark.parametrize("url", ["https://script.google.com/macros/s/AKfy/exec", "http://127.0.0.1:8000/exec", "http://localhost/exec"])
def test_good_urls_accepted(url):
    cfg = config.defaults()
    cfg["telemetry"]["endpoint"] = url
    assert config.validate(cfg) == []


@pytest.mark.parametrize("doms", [[], "tigeranalytics.com", ["a@b.com"], [""], ["x"]])
def test_allowed_domains_validation(doms):
    cfg = config.defaults()
    cfg["telemetry"]["allowed_domains"] = doms
    assert any("allowed_domains" in e for e in config.validate(cfg))


def test_events_validation():
    cfg = config.defaults()
    cfg["telemetry"]["events"] = ["sometimes"]
    assert any("events" in e for e in config.validate(cfg))


def test_two_api_urls_derive_from_the_endpoint_and_can_be_overridden():
    t = {"endpoint": "https://script.google.com/macros/s/X/exec", "register_url": "", "usage_url": ""}
    assert config.api_url(t, "register") == "https://script.google.com/macros/s/X/exec?api=register"
    assert config.api_url(t, "usage") == "https://script.google.com/macros/s/X/exec?api=usage"
    assert config.api_url({"endpoint": "https://h/exec?x=1"}, "usage") == "https://h/exec?x=1&api=usage"
    t["usage_url"] = "https://script.google.com/macros/s/Y/exec"
    assert config.api_url(t, "usage") == "https://script.google.com/macros/s/Y/exec"
    assert config.api_url(t, "register").endswith("?api=register")


def test_generated_hook_knows_both_urls_and_the_domain(tmp_path):
    write_cfg(tmp_path, endpoint="https://script.google.com/macros/s/X/exec")
    apply.init(tmp_path)
    src = read(tmp_path / ".claude/hooks/compact_report.py")
    assert '"https://script.google.com/macros/s/X/exec?api=register"' in src and '"https://script.google.com/macros/s/X/exec?api=usage"' in src
    assert '["tigeranalytics.com"]' in src
    compile(src, "compact_report.py", "exec")


def test_hook_entry_shape_and_matcher(tmp_path):
    write_cfg(tmp_path)
    apply.init(tmp_path)
    e = json.loads(read(tmp_path / ".claude/settings.json"))["hooks"]["PreCompact"][0]
    assert e["matcher"] == "auto"
    h = e["hooks"][0]
    assert h["async"] is True and h["args"] == ["${CLAUDE_PROJECT_DIR}/.claude/hooks/compact_report.py"]
    write_cfg(tmp_path, extra='events = ["auto", "manual"]\n')
    apply.init(tmp_path)
    assert "matcher" not in json.loads(read(tmp_path / ".claude/settings.json"))["hooks"]["PreCompact"][0]


def test_session_start_hook_is_registered_only_with_auto_register(tmp_path):
    write_cfg(tmp_path)
    apply.init(tmp_path)
    e = json.loads(read(tmp_path / ".claude/settings.json"))["hooks"]["SessionStart"][0]
    assert e["matcher"] == "startup|resume" and "async" not in e["hooks"][0]       # synchronous: it may hand Claude a sentence


def test_team_key_only_in_the_hook_script_and_only_with_auto_register(tmp_path):
    write_cfg(tmp_path)
    apply.init(tmp_path)
    holders = [p.name for p in (tmp_path / ".claude").rglob("*") if p.is_file() and KEY in read(p)]
    assert holders == ["compact_report.py"]
    write_cfg(tmp_path, extra="auto_register = false\n")
    apply.init(tmp_path)
    assert not any(KEY in read(p) for p in (tmp_path / ".claude").rglob("*") if p.is_file())
    assert "SessionStart" not in json.loads(read(tmp_path / ".claude/settings.json"))["hooks"]


def test_turning_telemetry_off_removes_hooks_and_script_keeps_user_hooks(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}))
    write_cfg(tmp_path)
    apply.init(tmp_path)
    s = json.loads(read(tmp_path / ".claude/settings.json"))
    assert len(s["hooks"]["PreCompact"]) == 2
    (tmp_path / "cckit.toml").write_text("[telemetry]\nenabled = false\n")
    apply.init(tmp_path)
    s = json.loads(read(tmp_path / ".claude/settings.json"))
    assert s["hooks"]["PreCompact"] == [{"hooks": [{"type": "command", "command": "mine.sh"}]}]
    assert "SessionStart" not in s["hooks"] and "PreToolUse" in s["hooks"]
    assert not (tmp_path / ".claude/hooks/compact_report.py").exists()


def test_old_manifest_with_hook_flag_still_upgrades(tmp_path):
    apply.init(tmp_path)
    m = json.loads(read(tmp_path / ".cckit/manifest.json"))
    m["settings"].pop("hooks", None)
    m["settings"]["hook"] = True                      # what version 0.1 wrote
    (tmp_path / ".cckit/manifest.json").write_text(json.dumps(m))
    apply.init(tmp_path)
    assert len(json.loads(read(tmp_path / ".claude/settings.json"))["hooks"]["PreToolUse"]) == 1


# ------------------------------------------------------------------ the Tiger e-mail rule
@pytest.mark.parametrize("email", [ADA, "ADA.Lovelace@TigerAnalytics.com", " ada@tigeranalytics.com ", "a+b@tigeranalytics.com", "a_b-c.d@tigeranalytics.com"])
def test_company_emails_accepted(email):
    assert identity.email_problem(email, [DOM]) is None


@pytest.mark.parametrize("email", ["ada@gmail.com", "ada@tigeranalytics.com.evil.io", "ada@evil-tigeranalytics.com", "ada@sub.tigeranalytics.com",
                                   "ada", "", "@tigeranalytics.com", "a b@tigeranalytics.com", "ada@tigeranalytics.co", "a@@tigeranalytics.com",
                                   "ada@tigeranalytics.com;x@evil.com", "ada@users.noreply.github.com"])
def test_other_emails_rejected_with_a_helpful_sentence(email):
    msg = identity.email_problem(email, [DOM])
    assert msg and "@tigeranalytics.com" in msg and "firstname.lastname@tigeranalytics.com" in msg


def test_several_domains_allowed():
    assert identity.email_problem("a@tigeranalytics.com", ["tigeranalytics.com", "tiger.example"]) is None
    assert identity.email_problem("a@tiger.example", ["tigeranalytics.com", "tiger.example"]) is None


def test_register_refuses_a_foreign_email_before_any_network_call(tmp_path, capsys):
    write_cfg(tmp_path)                                    # the endpoint is unreachable: a network call would say so
    assert cli.main(["register", str(tmp_path), "--email", "ada@gmail.com"]) == 2
    err = capsys.readouterr().err
    assert "@tigeranalytics.com" in err and "reach" not in err


def test_register_is_not_interactive_without_an_email(tmp_path, monkeypatch, capsys):
    write_cfg(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["register", str(tmp_path)]) == 2                  # no terminal and nothing to detect
    assert "--email" in capsys.readouterr().err


def test_register_asks_again_after_a_wrong_address(tmp_path, monkeypatch, capsys):
    write_cfg(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["ada@gmail.com", "nobody", "x@y.z"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["register", str(tmp_path)]) == 2                  # three wrong answers: gives up, never contacts the server
    assert capsys.readouterr().out.count("is not a @tigeranalytics.com address") == 3


def test_identity_file_is_private(ident):
    identity.save_identity("https://e/exec", ADA, "octocat", "tok")
    assert oct(ident.stat().st_mode & 0o777) == "0o600"
    assert identity.get_identity("https://e/exec") == {"email": ADA, "github": "octocat", "token": "tok"}
    assert identity.get_identity("https://other/exec") is None


# ------------------------------------------------------------------ finding the e-mail and the GitHub login without asking
def load_script(tmp_path, **overrides):
    """The generated hook script as a module, so its detection can be compared with cckit.identity."""
    write_cfg(tmp_path)
    apply.init(tmp_path)
    spec = importlib.util.spec_from_file_location("compact_report_under_test", tmp_path / ".claude/hooks/compact_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k, v in overrides.items():
        setattr(mod, k, v)
    return mod


@pytest.mark.skipif(not GIT, reason="needs git")
@pytest.mark.parametrize("email,expected", [(ADA, ADA), ("Ada.Lovelace@TigerAnalytics.com", ADA), ("ada@gmail.com", None),
                                            ("123+ada@users.noreply.github.com", None), (None, None)])
def test_email_detection_only_accepts_company_addresses(tmp_path, email, expected):
    repo = git_repo(tmp_path / "r", email=email)
    a = identity.detect_email(str(repo), [DOM])
    b = load_script(tmp_path).detect_email(str(repo))
    assert a == b and (a[0] if a else None) == expected


@pytest.mark.skipif(not GIT, reason="needs git")
def test_email_env_override_and_source_name(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "r", email=ADA)
    assert identity.detect_email(str(repo), [DOM]) == (ADA, "git config user.email")
    monkeypatch.setenv("CCKIT_EMAIL", "grace.hopper@tigeranalytics.com")
    assert identity.detect_email(str(repo), [DOM]) == ("grace.hopper@tigeranalytics.com", "CCKIT_EMAIL")
    monkeypatch.setenv("CCKIT_EMAIL", "grace@gmail.com")                           # a foreign override is ignored, git is used
    assert identity.detect_email(str(repo), [DOM]) == (ADA, "git config user.email")


def both_github(tmp_path, repo):
    a = identity.detect_github(str(repo))
    b = load_script(tmp_path).detect_github(str(repo))
    assert a == b, (a, b)
    return a


@pytest.mark.skipif(not GIT, reason="needs git")
def test_github_detection_sources_and_order(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "r")
    git(repo, "config", "user.name", "Ada Lovelace")                      # a display name is never used
    assert both_github(tmp_path, repo) is None
    git(repo, "remote", "add", "origin", "https://github.com/acme-org/app.git")     # the org is not the person
    git(repo, "remote", "add", "ssh", "git@github.com:acme-org/app.git")
    git(repo, "remote", "add", "bot", "https://x-access-token:ghs_SECRET@github.com/acme-org/app.git")
    assert both_github(tmp_path, repo) is None
    git(repo, "remote", "add", "me", "https://ada-dev@github.com/acme-org/app.git")
    assert both_github(tmp_path, repo) == ("ada-dev", "a git remote URL")
    monkeypatch.setenv("GITHUB_USER", "codespace-user")
    assert both_github(tmp_path, repo)[0] == "ada-dev"
    fake_gh(tmp_path, "gh-login")
    assert both_github(tmp_path, repo) == ("gh-login", "the GitHub CLI (gh)")
    git(repo, "config", "user.email", "4242+noreply-login@users.noreply.github.com")
    assert both_github(tmp_path, repo)[0] == "noreply-login"
    git(repo, "config", "github.user", "explicit")
    assert both_github(tmp_path, repo) == ("explicit", "git config github.user")
    monkeypatch.setenv("CCKIT_GITHUB", "override")
    assert both_github(tmp_path, repo) == ("override", "CCKIT_GITHUB")


@pytest.mark.skipif(not GIT, reason="needs git")
def test_tokens_in_remote_urls_are_never_returned(tmp_path):
    repo = git_repo(tmp_path / "r")
    git(repo, "remote", "add", "origin", "https://ghp_SECRETTOKEN@github.com/acme/app.git")
    got = identity.detect_github(str(repo))
    assert got is None or "SECRET" not in got[0]


# ------------------------------------------------------------------ the session metrics the hook reads from a transcript
def line(**kw):
    return json.dumps(kw)


def rich_transcript(path):
    """Streaming writes one line per content block of a message with growing usage; sub-agents are isSidechain; compactions are system lines."""
    common = {"version": "2.1.300", "entrypoint": "cli", "gitBranch": "feature/x", "sessionId": "sess-1"}
    rows = [
        line(type="user", message={"role": "user", "content": "SECRET PROMPT TEXT"}, timestamp="2026-07-01T10:00:00Z"),
        line(type="system", subtype="compact_boundary", content="Conversation compacted", timestamp="2026-07-01T10:01:00Z"),
        # message m1 written twice (two blocks): first line has partial usage, last line the final totals; one tool_use per block
        line(type="assistant", timestamp="2026-07-01T10:05:00Z", effort="medium", **common,
             message={"id": "m1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10},
                      "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat /etc/passwd SECRET"}}]}),
        line(type="assistant", timestamp="2026-07-01T10:05:01Z", effort="medium", **common,
             message={"id": "m1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10},
                      "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}, {"type": "text", "text": "SECRET ANSWER"}]}),
        line(type="assistant", isSidechain=True, timestamp="2026-07-01T10:06:00Z", **common,
             message={"id": "s1", "model": "claude-haiku-5-5", "usage": {"input_tokens": 7, "output_tokens": 20},
                      "content": [{"type": "tool_use", "id": "t3", "name": "Grep", "input": {}}]}),
        line(type="assistant", timestamp="2026-07-01T10:30:00Z", effort="high", **common,
             message={"id": "m2", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 500},
                      "content": [{"type": "tool_use", "id": "t4", "name": "Bash", "input": {}}]}),
        line(type="assistant", isSidechain=True, timestamp="2026-07-01T10:31:00Z", **common,
             message={"id": "s2", "model": "claude-haiku-5-5", "usage": {"input_tokens": 3, "output_tokens": 5}, "content": []}),
        line(type="assistant", timestamp="2026-07-01T10:32:00Z", **common,
             message={"id": "syn", "model": "<synthetic>", "usage": {"input_tokens": 0}, "content": []}),
        "not json at all",
    ]
    path.write_text("\n".join(rows) + "\n")
    return path


def test_session_metrics_from_a_transcript(tmp_path):
    mod = load_script(tmp_path)
    m = mod.session_metrics(str(rich_transcript(tmp_path / "t.jsonl")))
    assert m["model"] == "claude-sonnet-5-5" and m["effort"] == "high"                          # newest main-conversation message
    assert m["claude_version"] == "2.1.300" and m["entrypoint"] == "cli" and m["branch"] == "feature/x"
    assert m["context_tokens"] == 1200 + 30000 + 500
    assert (m["requests"], m["input_tokens"], m["output_tokens"]) == (2, 10 + 1200, 50 + 300)   # m1 counted once, with its FINAL usage
    assert (m["cache_read"], m["cache_write"]) == (100 + 30000, 10 + 500)
    assert (m["sub_requests"], m["sub_output"]) == (2, 25)
    assert m["tool_calls"] == 4 and m["tools"].split(";")[0] == "Bash:2" and set(m["tools"].split(";")) == {"Bash:2", "Read:1", "Grep:1"}
    assert m["models"] == "claude-sonnet-5-5:2;claude-haiku-5-5:2"
    assert m["compactions_before"] == 1 and m["session_minutes"] == 26 and m["partial"] is False
    assert "SECRET" not in json.dumps(m)


def test_huge_transcripts_are_read_from_the_tail_and_flagged(tmp_path):
    mod = load_script(tmp_path)
    tr = rich_transcript(tmp_path / "t.jsonl")
    mod.FULL_READ_LIMIT, mod.TAIL_BYTES = 100, 400
    m = mod.session_metrics(str(tr))
    assert m["partial"] is True and m["requests"] < 2


def test_metrics_of_a_missing_or_empty_transcript_are_zero(tmp_path):
    mod = load_script(tmp_path)
    for p in (None, str(tmp_path / "nope.jsonl")):
        assert mod.session_metrics(p)["requests"] == 0
    (tmp_path / "e.jsonl").write_text("")
    assert mod.session_metrics(str(tmp_path / "e.jsonl"))["model"] == ""


# ------------------------------------------------------------------ the two APIs, end to end against the real app
def fake_github(login):
    return {"nosuchuser": "No", "ratelimited": "Unchecked"}.get(login, "Yes")


APIS = {}      # base URL -> UsageApi, so the helpers below can read what the server stored


@pytest.fixture
def server(tmp_path_factory):
    """The usage app, listening on a free local port. Yields its base URL."""
    import threading
    api = usage_server.UsageApi(str(tmp_path_factory.mktemp("db") / "usage.db"), KEY, [DOM], admin_token=ADMIN, github_check=fake_github)
    srv = usage_server.make_server("127.0.0.1", 0, api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    APIS[base] = api
    yield base
    srv.shutdown()
    srv.server_close()


def dump(base):
    out = {}
    u, e = APIS[base].export("users"), APIS[base].export("events", 0, 2000)
    if u["rows"]:
        out["Claude_Users"] = [u["columns"]] + u["rows"]
    if e["rows"]:
        out["Claude_Compactions"] = [e["columns"]] + e["rows"]
    return out


def rows(base, tab):
    d = dump(base).get(tab) or [[]]
    return [dict(zip(d[0], r)) for r in d[1:]]


def clear_tokens(base, email):
    assert APIS[base].reset(email)                  # what `usage_api.py reset EMAIL` does


def devices(base, email):
    return int([r for r in rows(base, "Claude_Users") if r["Email"] == email][0]["Devices"])


def run_hook(repo, tr, trigger="auto", extra_env=None, cwd=None):
    payload = {"session_id": "sess-1", "transcript_path": str(tr), "cwd": str(cwd or repo / "myproject"), "hook_event_name": "PreCompact",
               "trigger": trigger, "custom_instructions": "do not leak this"}
    p = subprocess.run([sys.executable, str(repo / ".claude/hooks/compact_report.py")], input=json.dumps(payload), capture_output=True, text=True,
                       env={**os.environ, **(extra_env or {})}, timeout=30)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""     # silent: stdout would become compaction instructions
    return p


def setup(server, tmp_path, extra=""):
    write_cfg(tmp_path, endpoint=server + "/exec", extra=extra)
    apply.init(tmp_path)


def test_end_to_end_register_then_report(server, tmp_path):
    setup(server, tmp_path)
    assert cli.main(["whoami", str(tmp_path), "--check"]) == 0
    assert cli.main(["register", str(tmp_path), "--email", ADA, "--github", "octocat"]) == 0
    tok = identity.get_identity(server + "/exec")["token"]
    u = rows(server, "Claude_Users")[0]
    assert u["Email"] == ADA and u["GitHub"] == "octocat" and u["GitHub Verified"] == "Yes" and tok not in json.dumps(dump(server))   # only a hash
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"), cwd=tmp_path / "myproject")
    ev = rows(server, "Claude_Compactions")
    assert len(ev) == 1
    r = ev[0]
    assert (r["Email"], r["GitHub"], r["Trigger"]) == (ADA, "octocat", "auto")
    assert (r["Model"], r["Effort"], r["Claude Version"], r["Entrypoint"]) == ("claude-sonnet-5-5", "high", "2.1.300", "cli")
    assert (r["Context Tokens"], r["Window"], r["Context %"]) == (31700, 200000, 15.9)
    assert (r["Input Tokens"], r["Output Tokens"], r["Cache Read"], r["Cache Write"]) == (1210, 350, 30100, 510)
    assert r["Cache Hit %"] == round(30100 * 100 / (1210 + 30100 + 510), 1)
    assert (r["Requests"], r["Subagent Requests"], r["Subagent Output"], r["Tool Calls"]) == (2, 2, 25, 4)
    assert r["Models"] == "claude-sonnet-5-5:2;claude-haiku-5-5:2" and "Bash:2" in r["Tools"]
    assert (r["Session Minutes"], r["Compactions Before"], r["Project"], r["Branch"], r["Session"]) == (26, 1, "myproject", "feature/x", "sess-1")
    assert r["OS"] and r["Client"] == "cckit-hook/3"
    whole = json.dumps(dump(server))
    for secret in ("SECRET", "do not leak", str(tmp_path), "/etc/passwd"):
        assert secret not in whole
    assert rows(server, "Claude_Users")[0]["Compactions"] == 1 and rows(server, "Claude_Users")[0]["Last Seen"]


def test_project_and_branch_are_omitted_when_send_project_is_off(server, tmp_path):
    setup(server, tmp_path, extra="send_project = false\n")
    cli.main(["register", str(tmp_path), "--email", ADA])
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"))
    r = rows(server, "Claude_Compactions")[0]
    assert r["Project"] == "" and r["Branch"] == "" and r["Model"] == "claude-sonnet-5-5"


def test_manual_is_not_reported_unless_enabled(server, tmp_path):
    setup(server, tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"), trigger="manual")
    assert "Claude_Compactions" not in dump(server)


def test_hook_without_registration_or_server_is_silent(server, tmp_path):
    setup(server, tmp_path, extra="auto_register = false\n")
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"))            # not registered: nothing sent
    assert "Claude_Compactions" not in dump(server)
    write_cfg(tmp_path, endpoint="http://127.0.0.1:1/exec")              # nobody listens
    apply.init(tmp_path)
    identity.save_identity("http://127.0.0.1:1/exec", ADA, "", "tok")
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"))


def test_the_two_apis_are_separate_routes(server):
    post = lambda path, body: identity.post_json(server + path, body)
    reg = {"v": 1, "email": ADA, "team_key": KEY}
    assert post("/exec", reg)["ok"] is False and "unknown api" in post("/exec", reg)["error"]               # no ?api=
    assert "unknown api" in post("/exec?api=other", reg)["error"]
    token = post("/exec?api=register", reg)["token"]
    ev = {"v": 1, "email": ADA, "token": token, "event_id": "e1", "model": "m"}
    assert post("/exec?api=register", ev)["ok"] is False                      # an event is not a registration (no team key)
    assert post("/exec?api=usage", ev)["ok"] is True
    assert post("/exec?api=usage", reg)["ok"] is False                        # a registration is not an event
    assert len(rows(server, "Claude_Compactions")) == 1


def test_server_enforces_the_company_domain_even_if_the_client_is_bypassed(server):
    post = lambda email: identity.post_json(server + "/exec?api=register", {"v": 1, "email": email, "team_key": KEY})
    for bad in ("ada@gmail.com", "ada@tigeranalytics.com.evil.io", "ada@sub.tigeranalytics.com", "nobody", "", None):
        r = post(bad)
        assert r["ok"] is False and "@tigeranalytics.com" in r["error"], bad
    assert "Claude_Users" not in dump(server)
    assert post("ADA.Lovelace@TigerAnalytics.com")["ok"] is True
    assert rows(server, "Claude_Users")[0]["Email"] == ADA                    # stored lower-case


def test_registration_rules(server, tmp_path, capsys):
    setup(server, tmp_path)
    assert cli.main(["register", str(tmp_path), "--email", ADA, "--team-key", "wrong"]) == 1
    assert "wrong team key" in capsys.readouterr().err
    assert cli.main(["register", str(tmp_path), "--email", ADA]) == 0           # no GitHub login is fine
    assert rows(server, "Claude_Users")[0]["GitHub"] == ""
    for _ in range(4):                                                          # more machines of the same developer, 5 in all
        assert cli.main(["register", str(tmp_path), "--email", ADA.upper()]) == 0      # case-insensitive: still one row
    assert len(rows(server, "Claude_Users")) == 1 and devices(server, ADA) == 5
    assert cli.main(["register", str(tmp_path), "--email", ADA]) == 1           # the 6th needs an admin
    assert "5 machines" in capsys.readouterr().err


def test_github_is_optional_and_never_blocks_registration(server, tmp_path):
    setup(server, tmp_path)
    assert cli.main(["register", str(tmp_path), "--email", "a.one@tigeranalytics.com", "--github", "nosuchuser"]) == 0
    assert cli.main(["register", str(tmp_path), "--email", "b.two@tigeranalytics.com", "--github", "ratelimited"]) == 0
    assert cli.main(["register", str(tmp_path), "--email", "c.three@tigeranalytics.com", "--github", "bad name!"]) == 0
    got = {r["Email"]: (r["GitHub"], r["GitHub Verified"]) for r in rows(server, "Claude_Users")}
    assert got == {"a.one@tigeranalytics.com": ("nosuchuser", "No"), "b.two@tigeranalytics.com": ("ratelimited", "Unchecked"),
                   "c.three@tigeranalytics.com": ("", "")}


def test_wrong_token_and_bad_input_rejected(server, tmp_path):
    setup(server, tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    url = server + "/exec?api=usage"
    ok = lambda **kw: identity.post_json(url, {"v": 1, "email": ADA, "event_id": "e" + str(time.time_ns()), **kw})
    tok = identity.get_identity(server + "/exec")["token"]
    assert ok(token="nope")["ok"] is False
    assert ok(token=tok, trigger="auto", model="=HYPERLINK(\"http://evil\")", project="+cmd", branch="@x", context_tokens=-5, window="x")["ok"] is True
    r = rows(server, "Claude_Compactions")[0]
    assert r["Model"].startswith("'=") and r["Project"].startswith("'+") and r["Branch"].startswith("'@")     # formulas neutralised
    assert r["Context Tokens"] == 0 and r["Window"] == 0 and r["Context %"] == ""
    e1 = {"v": 1, "email": ADA, "token": tok, "event_id": "same-id"}
    assert identity.post_json(url, e1)["ok"] is True
    assert identity.post_json(url, e1).get("duplicate") is True                # replay dropped
    assert identity.post_json(url, {"v": 1, "email": "ghost@tigeranalytics.com", "token": "x", "event_id": "z"})["ok"] is False
    assert identity.post_json(url, {"v": 1, "email": "x@gmail.com", "token": tok, "event_id": "z2"})["ok"] is False
    assert identity.post_json(url, {"v": 1, "email": ADA, "token": tok})["ok"] is False                      # event_id is required
    assert identity.post_json(url, {"v": 1, "email": ADA, "token": tok, "event_id": "big", "tools": "x" * 7000})["ok"] is False   # too large


def test_each_machine_keeps_its_own_token_and_admin_can_reset(server, tmp_path):
    setup(server, tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    laptop = identity.get_identity(server + "/exec")["token"]
    cli.main(["register", str(tmp_path), "--email", ADA])                       # a second machine
    devbox = identity.get_identity(server + "/exec")["token"]
    assert laptop != devbox
    ev = lambda t, i: identity.post_json(server + "/exec?api=usage", {"v": 1, "email": ADA, "token": t, "event_id": i})["ok"]
    assert ev(laptop, "a") and ev(devbox, "b")
    clear_tokens(server, ADA)                                                   # an admin resets the developer's machines
    assert not ev(laptop, "c") and not ev(devbox, "d")


def test_hourly_event_limit(server, tmp_path):
    setup(server, tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    tok = identity.get_identity(server + "/exec")["token"]
    answers = [identity.post_json(server + "/exec?api=usage", {"v": 1, "email": ADA, "token": tok, "event_id": f"id{i}"})["ok"] for i in range(123)]
    assert answers[:120] == [True] * 120 and answers[120:] == [False] * 3


# ------------------------------------------------------------------ the SessionStart hook, end to end
def session_start(repo, env_extra=None):
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}
    p = subprocess.run([sys.executable, str(repo / ".claude/hooks/compact_report.py")], input=json.dumps(payload), capture_output=True,
                       text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), **(env_extra or {})}, timeout=60)
    assert p.returncode == 0 and p.stderr == ""
    return json.loads(p.stdout) if p.stdout.strip() else None


@pytest.mark.skipif(not GIT, reason="needs git")
def test_session_start_registers_from_the_git_email_and_tells_the_user(server, tmp_path):
    setup(server, tmp_path)
    git_repo(tmp_path, email=ADA, gh_user="octocat")
    out = session_start(tmp_path)
    assert f"registered {ADA} (taken from git config user.email)" in out["systemMessage"] and "GitHub login 'octocat'" in out["systemMessage"]
    assert "hookSpecificOutput" not in out                                  # Claude is not bothered
    assert identity.get_identity(server + "/exec")["email"] == ADA
    assert rows(server, "Claude_Users")[0]["GitHub"] == "octocat"
    assert session_start(tmp_path) is None                                  # already registered: silent
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"), cwd=tmp_path)
    assert rows(server, "Claude_Compactions")[0]["Email"] == ADA


@pytest.mark.skipif(not GIT, reason="needs git")
def test_session_start_ignores_a_personal_git_email_and_asks(server, tmp_path):
    setup(server, tmp_path)
    git_repo(tmp_path, email="ada.private@gmail.com")
    out = session_start(tmp_path)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "company e-mail" in ctx and "@tigeranalytics.com" in ctx and "--register THEIR_EMAIL" in ctx and str(tmp_path) in ctx
    assert KEY not in ctx and "gmail" not in ctx
    assert "Claude_Users" not in dump(server)                               # the personal address was never sent anywhere
    assert session_start(tmp_path) is None                                  # asked recently: not again for a few days


def test_script_register_command_accepts_a_company_email_only(server, tmp_path):
    setup(server, tmp_path)
    cmd = lambda *a: subprocess.run([sys.executable, str(tmp_path / ".claude/hooks/compact_report.py"), *a], capture_output=True, text=True, env=os.environ)
    bad = cmd("--register", "ada@gmail.com")
    assert bad.returncode == 1 and "@tigeranalytics.com" in bad.stdout and "Claude_Users" not in dump(server)
    good = cmd("--register", ADA, "--github", "octocat")
    assert good.returncode == 0 and f"Registered {ADA}" in good.stdout
    assert identity.get_identity(server + "/exec")["email"] == ADA and rows(server, "Claude_Users")[0]["GitHub"] == "octocat"


@pytest.mark.skipif(not GIT, reason="needs git")
def test_session_start_is_silent_and_retries_later_when_server_is_down(tmp_path):
    write_cfg(tmp_path, endpoint="http://127.0.0.1:1/exec")
    apply.init(tmp_path)
    git_repo(tmp_path, email=ADA)
    assert session_start(tmp_path) is None
    assert not (tmp_path / "home" / "asked.json").exists()                  # nothing was asked, so nothing is throttled


@pytest.mark.skipif(not GIT, reason="needs git")
def test_opt_out_environment_variable(server, tmp_path):
    setup(server, tmp_path)
    git_repo(tmp_path, email=ADA)
    assert session_start(tmp_path, {"CCKIT_TELEMETRY": "off"}) is None
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"), extra_env={"CCKIT_TELEMETRY": "off"}, cwd=tmp_path)
    assert "Claude_Users" not in dump(server)


@pytest.mark.skipif(not GIT, reason="needs git")
def test_precompact_registers_late_if_session_start_never_ran(server, tmp_path):
    setup(server, tmp_path)
    git_repo(tmp_path, email=ADA)
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"), cwd=tmp_path)
    assert rows(server, "Claude_Compactions")[0]["Email"] == ADA


@pytest.mark.skipif(not GIT, reason="needs git")
def test_cckit_init_registers_from_git_without_a_terminal(server, tmp_path, capsys, monkeypatch):
    write_cfg(tmp_path, endpoint=server + "/exec")
    git_repo(tmp_path, email=ADA, gh_user="octocat")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["init", str(tmp_path)]) == 0
    assert f"Using {ADA} (from git config user.email)" in capsys.readouterr().out
    assert identity.get_identity(server + "/exec")["email"] == ADA


# ------------------------------------------------------------------ the server files ship in the package
# ------------------------------------------------------------------ a deployment that wants a Google sign-in is explained, not just "failed"
@pytest.fixture
def login_page_server():
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def _page(self):
            body = b"<!DOCTYPE html><html><head><title>Sign in - Google Accounts</title></head></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST = lambda self: self._page()
        log_message = lambda *a: None

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/exec"
    srv.shutdown()


def test_ping_and_register_explain_a_sign_in_page(login_page_server, tmp_path, capsys):
    with pytest.raises(identity.RegisterError, match="web page.*usage app"):
        identity.ping(login_page_server)
    write_cfg(tmp_path, endpoint=login_page_server)
    assert cli.main(["whoami", str(tmp_path), "--check"]) == 1
    assert "web page" in capsys.readouterr().out
    assert cli.main(["register", str(tmp_path), "--email", ADA]) == 1
    assert "web page" in capsys.readouterr().err


# ------------------------------------------------------------------ `cckit server`: things only the standalone server has
def new_api(tmp_path, **kw):
    kw.setdefault("github_check", fake_github)
    return usage_server.UsageApi(str(tmp_path / "u.db"), KEY, [DOM], admin_token=kw.pop("admin_token", ADMIN), **kw)


def http_get(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token} if token else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def pyserver(server):
    return server, APIS[server]


def seed(base, n_events, email=ADA):
    tok = identity.post_json(base + "/?api=register", {"email": email, "team_key": KEY, "github": "octocat"})["token"]
    for i in range(n_events):
        assert identity.post_json(base + "/?api=usage", {"email": email, "token": tok, "event_id": f"ev{i}", "model": "m", "context_tokens": 10})["ok"]
    return tok


def test_export_needs_the_admin_token_and_never_shows_token_hashes(pyserver):
    base, api = pyserver
    tok = seed(base, 2)
    assert http_get(base + "/?api=export&table=events")[0] == 401
    assert http_get(base + "/?api=export&table=events", "wrong")[0] == 401
    code, d = http_get(base + "/?api=export&table=users", ADMIN)
    assert code == 200 and d["columns"][4] == "Devices" and d["rows"][0][4] == 1
    assert tok not in json.dumps(d) and sha_of(tok) not in json.dumps(d)
    assert http_get(base + "/?api=export&table=nope", ADMIN)[1]["ok"] is False


def sha_of(t):
    import hashlib
    return hashlib.sha256(t.encode()).hexdigest()


def test_export_is_disabled_without_an_admin_token(tmp_path):
    api = new_api(tmp_path, admin_token="")
    assert api.handle("GET", {"api": ["export"]}, b"", "Bearer ")[0] == 403
    assert api.handle("GET", {"api": ["export"]}, b"", "")[0] == 403


def test_export_pages_by_cursor_without_gaps_or_repeats(pyserver):
    base, _ = pyserver
    seed(base, 7)
    seen, after = [], 0
    for _ in range(10):
        _, d = http_get(base + f"/?api=export&table=events&after={after}&limit=3", ADMIN)
        seen += [r[1] for r in d["rows"]]
        after = d["last_id"]
        if not d["more"]:
            break
    assert seen == [f"ev{i}" for i in range(7)]
    assert http_get(base + f"/?api=export&table=events&after={after}", ADMIN)[1]["rows"] == []


def test_data_survives_a_restart(tmp_path):
    a = new_api(tmp_path)
    tok = a.register({"email": ADA, "team_key": KEY})["token"]
    b = new_api(tmp_path)                                        # a new process on the same file
    assert b.usage({"email": ADA, "token": tok, "event_id": "x"}) == {"ok": True}


def test_registration_limit_is_per_hour_and_refused_attempts_count(tmp_path):
    t = [1_000_000.0]
    api = new_api(tmp_path, register_per_hour=2, clock=lambda: t[0])
    assert api.register({"email": "a.one@tigeranalytics.com", "team_key": KEY})["ok"]
    assert api.register({"email": "b.two@tigeranalytics.com", "team_key": KEY})["ok"]
    with pytest.raises(usage_server.ApiError, match="too many registrations"):
        api.register({"email": "c.three@tigeranalytics.com", "team_key": KEY})
    t[0] += 3700                                                  # next hour
    assert api.register({"email": "c.three@tigeranalytics.com", "team_key": KEY})["ok"]


def test_concurrent_events_are_all_stored(pyserver):
    import threading
    base, api = pyserver
    tok = identity.post_json(base + "/?api=register", {"email": ADA, "team_key": KEY})["token"]
    results = []

    def send(i):
        results.append(identity.post_json(base + "/?api=usage", {"email": ADA, "token": tok, "event_id": f"c{i}"})["ok"])

    threads = [threading.Thread(target=send, args=(i,)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [True] * 30 and len(api.export("events", 0, 100)["rows"]) == 30
    assert api.export("users")["rows"][0][7] == 30


@pytest.mark.parametrize("body,fragment", [(b"not json", "JSON"), (b"[1,2]", "JSON object"), (b"\xff\xfe", "JSON"), (b"x" * 7000, "too large")])
def test_bad_bodies_get_a_clear_answer(tmp_path, body, fragment):
    api = new_api(tmp_path)
    code, d = api.handle("POST", {"api": ["register"]}, body)
    assert code == 200 and d["ok"] is False and fragment in d["error"]


def test_huge_body_is_refused_without_being_read(pyserver):
    base, _ = pyserver
    r = identity.post_json(base + "/?api=usage", {"email": ADA, "token": "x", "event_id": "e", "tools": "x" * 70000})
    assert r["ok"] is False and "too large" in r["error"]


def test_unexpected_errors_never_leak_internals(tmp_path):
    api = new_api(tmp_path)
    api.register = lambda b: 1 / 0
    code, d = api.handle("POST", {"api": ["register"]}, b"{}")
    assert (code, d) == (500, {"ok": False, "error": "server error"})


def test_a_switched_off_developer_is_refused_everywhere(pyserver, capsys, monkeypatch, tmp_path):
    base, api = pyserver
    tok = seed(base, 1)
    assert api.set_active(ADA, False)
    assert "switched off" in identity.post_json(base + "/?api=usage", {"email": ADA, "token": tok, "event_id": "z"})["error"]
    assert "switched off" in identity.post_json(base + "/?api=register", {"email": ADA, "team_key": KEY})["error"]
    assert api.set_active(ADA, True)
    assert identity.post_json(base + "/?api=usage", {"email": ADA, "token": tok, "event_id": "z"})["ok"] is True
    assert not api.set_active("nobody@tigeranalytics.com", False)


def test_server_cli_commands(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "cli.db")
    monkeypatch.setenv("CCKIT_DB", db)
    monkeypatch.setenv("CCKIT_TEAM_KEY", KEY)
    api = usage_server.UsageApi(db, KEY, [DOM], github_check=fake_github)
    api.register({"email": ADA, "team_key": KEY, "github": "octocat"})
    assert cli.main(["server", "users"]) == 0 and ADA in capsys.readouterr().out
    assert cli.main(["server", "deactivate", ADA]) == 0 and api.export("users")["rows"][0][5] == "No"
    assert cli.main(["server", "activate", ADA]) == 0 and api.export("users")["rows"][0][5] == "Yes"
    assert cli.main(["server", "reset", ADA]) == 0 and api.export("users")["rows"][0][4] == 0
    assert cli.main(["server", "reset", "ghost@tigeranalytics.com"]) == 1
    capsys.readouterr()
    assert cli.main(["server", "keys"]) == 0
    out = capsys.readouterr().out
    assert "CCKIT_TEAM_KEY=" in out and "CCKIT_ADMIN_TOKEN=" in out


def test_server_run_needs_a_team_key(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("CCKIT_TEAM_KEY", raising=False)
    monkeypatch.setenv("CCKIT_DB", str(tmp_path / "x.db"))
    assert cli.main(["server", "run"]) == 2
    assert "CCKIT_TEAM_KEY" in capsys.readouterr().err


def test_server_logs_nothing_from_requests(pyserver, capfd):
    base, _ = pyserver
    seed(base, 1)
    out = capfd.readouterr()
    assert "token" not in out.err and "event_id" not in out.err and "usage" not in out.err


def test_client_registers_against_the_standalone_server_with_the_default_urls(pyserver, tmp_path):
    base, _ = pyserver
    write_cfg(tmp_path, endpoint=base + "/")                       # any path works: routing is by ?api=
    assert cli.main(["register", str(tmp_path), "--email", ADA]) == 0
    assert identity.get_identity(base + "/")["email"] == ADA
    assert cli.main(["whoami", str(tmp_path), "--check"]) == 0


# ------------------------------------------------------------------ CSV export, WSGI, dump, and the deployable app folder
def http_text(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token} if token else {})
    with urllib.request.urlopen(req) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode("utf-8")


def test_csv_export_has_the_same_columns_and_needs_the_admin_token(pyserver):
    import csv as csvmod
    import io
    base, _ = pyserver
    seed(base, 2)
    status, ctype, body = http_text(base + "/?api=export&table=events&format=csv", ADMIN)
    assert status == 200 and ctype.startswith("text/csv")
    rows_ = list(csvmod.reader(io.StringIO(body)))
    assert rows_[0][:3] == ["Received At", "Event ID", "Email"] and [r[1] for r in rows_[1:]] == ["ev0", "ev1"]
    assert http_get(base + "/?api=export&table=events&format=csv")[0] == 401                  # no token, no data, whatever the format
    assert "ev0" not in json.dumps(http_get(base + "/?api=export&table=events&format=csv", "wrong")[1])


def test_csv_cells_that_look_like_formulas_stay_neutralised(pyserver):
    base, _ = pyserver
    tok = seed(base, 0)
    identity.post_json(base + "/?api=usage", {"email": ADA, "token": tok, "event_id": "f", "model": "=cmd|' /C calc'!A0", "project": "@SUM(1)", "branch": "-2+3"})
    body = http_text(base + "/?api=export&table=events&format=csv", ADMIN)[2]
    assert ",'=cmd" in body and ",'@SUM" in body and ",'-2+3" in body


def wsgi_call(app, method="GET", query="", body=b"", auth="", path="/"):
    import io
    out = {}
    environ = {"REQUEST_METHOD": method, "QUERY_STRING": query, "PATH_INFO": path, "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body), "HTTP_AUTHORIZATION": auth}
    chunks = app(environ, lambda status, headers: out.update(status=status, headers=dict(headers)))
    return out["status"], out["headers"], b"".join(chunks)


def test_wsgi_entry_point_serves_the_same_api(tmp_path, monkeypatch):
    monkeypatch.setenv("CCKIT_TEAM_KEY", KEY)
    monkeypatch.setenv("CCKIT_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("CCKIT_DB", str(tmp_path / "w.db"))
    monkeypatch.setattr(usage_server, "_APP", None)
    s, h, b = wsgi_call(usage_server.application, query="api=ping")
    assert s.startswith("200") and json.loads(b)["ready"] is True
    assert json.loads(wsgi_call(usage_server.application, path="/healthz")[2]) == {"ok": True}
    s, h, b = wsgi_call(usage_server.application, "POST", "api=register", json.dumps({"email": ADA, "team_key": KEY}).encode())
    tok = json.loads(b)["token"]
    assert json.loads(wsgi_call(usage_server.application, "POST", "api=usage", json.dumps({"email": ADA, "token": tok, "event_id": "w1"}).encode())[2]) == {"ok": True}
    assert wsgi_call(usage_server.application, query="api=export&table=events", auth="Bearer nope")[0].startswith("401")
    s, h, b = wsgi_call(usage_server.application, query="api=export&table=events&format=csv", auth="Bearer " + ADMIN)
    assert h["Content-Type"].startswith("text/csv") and b"w1" in b
    big = wsgi_call(usage_server.application, "POST", "api=usage", b"x" * 70000)
    assert json.loads(big[2])["error"] == "payload too large"


def test_wsgi_without_configuration_fails_safely(monkeypatch, capsys):
    monkeypatch.delenv("CCKIT_TEAM_KEY", raising=False)
    monkeypatch.setattr(usage_server, "_APP", None)
    s, h, b = wsgi_call(usage_server.application, query="api=ping")
    assert s.startswith("500") and json.loads(b) == {"ok": False, "error": "server error"}
    assert "CCKIT_TEAM_KEY" in capsys.readouterr().err                       # the log says why; the caller does not learn it


def test_the_same_event_arriving_at_two_processes_is_stored_once(tmp_path):
    import threading
    a, b = new_api(tmp_path), new_api(tmp_path)                               # two instances = two processes: separate locks, one database
    tok = a.register({"email": ADA, "team_key": KEY})["token"]
    results = []

    def go(api, i):
        results.append(api.usage({"email": ADA, "token": tok, "event_id": f"dup{i % 5}"}))

    ts = [threading.Thread(target=go, args=(a if i % 2 else b, i)) for i in range(20)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert all(r["ok"] for r in results) and len(a.export("events", 0, 100)["rows"]) == 5


def test_dump_command_writes_csv(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "d.db")
    monkeypatch.setenv("CCKIT_DB", db)
    monkeypatch.delenv("CCKIT_TEAM_KEY", raising=False)                       # admin commands need no key
    api = usage_server.UsageApi(db, KEY, [DOM], github_check=fake_github)
    tok = api.register({"email": ADA, "team_key": KEY})["token"]
    for i in range(3):
        api.usage({"email": ADA, "token": tok, "event_id": f"d{i}", "model": "m"})
    out = tmp_path / "events.csv"
    assert cli.main(["server", "dump", "--table", "events", "--out", str(out)]) == 0
    assert read(out).count("\n") == 4 and "d2" in read(out)
    capsys.readouterr()
    assert cli.main(["server", "dump", "--table", "users"]) == 0
    assert ADA in capsys.readouterr().out
    assert cli.main(["server", "users"]) == 0


def test_export_app_writes_a_deployable_folder_and_check_notices_drift(tmp_path, capsys):
    out = tmp_path / "usage-api"
    assert cli.main(["server", "export-app", "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == [".dockerignore", ".env.example", ".gitignore", "Dockerfile", "README.md", "usage_api.py"]
    assert cli.main(["server", "export-app", "--check", "--out", str(out)]) == 0
    (out / "usage_api.py").write_text("# edited\n")
    assert cli.main(["server", "export-app", "--check", "--out", str(out)]) == 1
    assert "usage_api.py" in capsys.readouterr().out


def test_the_app_file_needs_nothing_from_cckit(tmp_path):
    out = tmp_path / "app"
    cli.main(["server", "export-app", "--out", str(out)])
    src = read(out / "usage_api.py")
    assert "import cckit" not in src and "from cckit" not in src and "from . import" not in src
    assert read(out / "usage_api.py") == read(Path(usage_server.__file__))    # exactly the module the tests exercise
    assert "CCKIT_TEAM_KEY=\n" in read(out / ".env.example") and "gunicorn" in read(out / "README.md")


def test_the_exported_app_runs_on_its_own_and_serves_the_client(tmp_path):
    """Run usage_api.py as a separate process, outside the cckit package, and register + report through the real client."""
    import socket
    out = tmp_path / "app"
    cli.main(["server", "export-app", "--out", str(out)])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {"PATH": os.environ["PATH"], "CCKIT_TEAM_KEY": KEY, "CCKIT_ADMIN_TOKEN": ADMIN, "CCKIT_DB": str(tmp_path / "own.db")}
    proc = subprocess.Popen([sys.executable, "-I", str(out / "usage_api.py"), "run", "--port", str(port)], env=env, cwd=str(tmp_path),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                if http_get(base + "/?api=ping")[1]["ready"]:
                    break
            except OSError:
                time.sleep(0.1)
        write_cfg(tmp_path, endpoint=base + "/")
        assert cli.main(["whoami", str(tmp_path), "--check"]) == 0
        assert cli.main(["register", str(tmp_path), "--email", ADA]) == 0
        tok = identity.get_identity(base + "/")["token"]
        assert identity.post_json(base + "/?api=usage", {"email": ADA, "token": tok, "event_id": "own1", "model": "m"}) == {"ok": True}
        assert "own1" in http_text(base + "/?api=export&table=events&format=csv", ADMIN)[2]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ------------------------------------------------------------------ URLs from the config file: endpoint is optional when both API URLs are given
URLS = {"endpoint": "", "register_url": "https://app.example.com/api/register", "usage_url": "https://app.example.com/api/usage", "ping_url": ""}


def test_ping_url_is_derived_from_the_sibling_urls_unless_given():
    assert config.api_url(URLS, "ping") == "https://app.example.com/api/ping"
    assert config.api_url(dict(URLS, ping_url="https://app.example.com/health"), "ping") == "https://app.example.com/health"
    assert config.api_url(dict(URLS, register_url="https://app.example.com/signup", usage_url="https://app.example.com/send"), "ping") == ""
    assert config.api_url({"endpoint": "https://h/", "register_url": "", "usage_url": "", "ping_url": ""}, "ping") == "https://h/?api=ping"
    assert config.api_url({"endpoint": "", "register_url": "", "usage_url": "", "ping_url": ""}, "register") == ""


def test_identity_key_is_the_endpoint_or_the_usage_url():
    assert config.identity_key(URLS) == "https://app.example.com/api/usage"
    assert config.identity_key(dict(URLS, endpoint="https://app.example.com/")) == "https://app.example.com/"
    assert config.identity_key({"endpoint": "", "usage_url": ""}) == ""


def test_both_urls_without_an_endpoint_switch_telemetry_on(tmp_path):
    (tmp_path / "cckit.toml").write_text('[telemetry]\nenabled = true\nregister_url = "https://app.example.com/api/register"\nusage_url = "https://app.example.com/api/usage"\nteam_key = "k"\n')
    apply.init(tmp_path)
    src = read(tmp_path / ".claude/hooks/compact_report.py")
    assert '"https://app.example.com/api/register"' in src and '"https://app.example.com/api/usage"' in src
    assert 'ENDPOINT = "https://app.example.com/api/usage"' in src                  # the identity key
    s = json.loads(read(tmp_path / ".claude/settings.json"))
    assert "PreCompact" in s["hooks"] and "SessionStart" in s["hooks"]
    assert not any(lv in ("warn", "fail") and "URL" in t for lv, t in doctor.run(tmp_path, check_claude=False).items)


def test_only_one_of_the_two_urls_is_not_enough(tmp_path):
    (tmp_path / "cckit.toml").write_text('[telemetry]\nenabled = true\nregister_url = "https://app.example.com/api/register"\nusage_url = ""\n')
    apply.init(tmp_path)
    assert "PreCompact" not in json.loads(read(tmp_path / ".claude/settings.json"))["hooks"]
    assert any(lv == "warn" and "no URL is set" in t for lv, t in doctor.run(tmp_path, check_claude=False).items)


def test_changing_the_domain_means_a_new_identity_key(tmp_path):
    identity.save_identity("https://old.example.com/api/usage", ADA, "", "tok")
    t = dict(URLS, usage_url="https://new.example.com/api/usage", register_url="https://new.example.com/api/register")
    assert identity.get_identity(config.identity_key(t)) is None                    # not registered on the new app yet: the hook registers again
    assert identity.get_identity("https://old.example.com/api/usage")["token"] == "tok"


def test_end_to_end_with_path_style_urls_and_no_endpoint(server, tmp_path, capsys):
    (tmp_path / "cckit.toml").write_text(f'[telemetry]\nenabled = true\nregister_url = "{server}/api/register"\nusage_url = "{server}/api/usage"\nteam_key = "{KEY}"\n')
    apply.init(tmp_path)
    assert cli.main(["whoami", str(tmp_path), "--check"]) == 0
    out = capsys.readouterr().out
    assert f"ping:       {server}/api/ping" in out and "check:      ok" in out
    assert cli.main(["register", str(tmp_path), "--email", ADA]) == 0
    assert identity.get_identity(f"{server}/api/usage")["email"] == ADA
    run_hook(tmp_path, rich_transcript(tmp_path / "t.jsonl"))
    assert rows(server, "Claude_Compactions")[0]["Email"] == ADA
    assert cli.main(["doctor", str(tmp_path), "--no-claude"]) == 0


@pytest.mark.skipif(not GIT, reason="needs git")
def test_session_start_registers_through_path_style_urls(server, tmp_path):
    (tmp_path / "cckit.toml").write_text(f'[telemetry]\nenabled = true\nregister_url = "{server}/api/register"\nusage_url = "{server}/api/usage"\nteam_key = "{KEY}"\n')
    apply.init(tmp_path)
    git_repo(tmp_path, email=ADA)
    assert "registered" in session_start(tmp_path)["systemMessage"]
    assert identity.get_identity(f"{server}/api/usage")["email"] == ADA


def test_server_routes_by_path_as_well_as_by_query(server):
    reg = identity.post_json(server + "/anything/register", {"email": ADA, "team_key": KEY})
    assert reg["ok"] is True
    assert identity.post_json(server + "/a/b/usage", {"email": ADA, "token": reg["token"], "event_id": "p1"}) == {"ok": True}
    assert http_get(server + "/api/ping")[1]["ready"] is True
    assert identity.post_json(server + "/api/nothing", {"x": 1})["error"].startswith("unknown api")
    assert http_get(server + "/api/export?table=users", ADMIN)[1]["ok"] is True


# ------------------------------------------------------------------ `cckit init` in a real terminal: it asks for the e-mail
def run_in_pty(args, inputs, timeout=90):
    """Run `cckit <args>` on a pseudo-terminal, answering its prompts with `inputs` in order. Returns (exit code, everything it printed)."""
    import pty
    import select
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execve(sys.executable, [sys.executable, "-m", "cckit.cli", *args], dict(os.environ))
        finally:
            os._exit(127)
    for line in inputs:
        os.write(fd, (line + "\n").encode())
    out, end = b"", time.time() + timeout
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 1)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break                                              # the child closed the terminal
            if not chunk:
                break
            out += chunk
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace").replace("\r\n", "\n")


def init_repo(tmp_path, server, email=ADA, toml_extra="", with_key=False):
    git_repo(tmp_path, email=email)
    (tmp_path / "cckit.toml").write_text(f'[telemetry]\nenabled = true\nendpoint = "{server}/exec"\n' + (f'team_key = "{KEY}"\n' if with_key else "") + toml_extra)
    return tmp_path


@pytest.mark.skipif(not GIT, reason="needs git")
def test_init_asks_for_the_email_offering_the_git_one_then_for_the_team_key(server, tmp_path):
    repo = init_repo(tmp_path, server)
    code, out = run_in_pty(["init", str(repo)], ["", KEY, "n"])
    assert code == 0
    assert f"Your company e-mail (@tigeranalytics.com) [{ADA}, from git config user.email]:" in out
    assert "Team key (from your admin):" in out and f"Registered {ADA}" in out
    assert "Save the team key?" in out and "Not saved" in out
    assert identity.get_identity(server + "/exec")["email"] == ADA
    assert KEY not in read(repo / "cckit.toml") and not any(KEY in read(p) for p in (repo / ".claude").rglob("*.py"))      # not saved: not committed


@pytest.mark.skipif(not GIT, reason="needs git")
def test_init_can_save_the_team_key_so_teammates_register_automatically(server, tmp_path):
    repo = init_repo(tmp_path, server)
    code, out = run_in_pty(["init", str(repo)], ["", KEY, "y"])
    assert code == 0 and "Saved to cckit.toml; the hooks were regenerated" in out
    assert f'team_key = "{KEY}"' in read(repo / "cckit.toml")
    assert KEY in read(repo / ".claude/hooks/compact_report.py")
    assert "SessionStart" in json.loads(read(repo / ".claude/settings.json"))["hooks"]          # now there is a key to register others with
    assert cli.main(["doctor", str(repo), "--no-claude"]) == 0


@pytest.mark.skipif(not GIT, reason="needs git")
def test_init_accepts_a_different_company_email_than_git_has(server, tmp_path):
    repo = init_repo(tmp_path, server)
    code, out = run_in_pty(["init", str(repo)], ["other.person@tigeranalytics.com", KEY, "n"])
    assert code == 0 and "Registered other.person@tigeranalytics.com" in out
    assert identity.get_identity(server + "/exec")["email"] == "other.person@tigeranalytics.com"


@pytest.mark.skipif(not GIT, reason="needs git")
def test_init_does_not_fail_when_the_team_key_is_wrong(server, tmp_path):
    repo = init_repo(tmp_path, server)
    code, out = run_in_pty(["init", str(repo)], ["", "wrong-key"])
    assert code == 0                                                           # the files were written: init itself worked
    assert "wrong team key" in out and "Registration did not complete" in out and "Save the team key?" not in out
    assert identity.get_identity(server + "/exec") is None
    assert (repo / ".claude/settings.json").exists()


@pytest.mark.skipif(not GIT, reason="needs git")
def test_init_with_no_company_email_in_git_asks_and_refuses_personal_addresses(server, tmp_path):
    repo = init_repo(tmp_path, server, email="me@gmail.com")
    code, out = run_in_pty(["init", str(repo)], ["a@gmail.com", "b@yahoo.com", "c@x.org"])
    assert code == 0 and out.count("is not a @tigeranalytics.com address") == 3 and "Registration did not complete" in out
    assert "from git config" not in out.split("Your company e-mail")[1].split(":")[0]            # no default offered: the personal address is never suggested
    assert identity.get_identity(server + "/exec") is None


@pytest.mark.skipif(not GIT, reason="needs git")
def test_init_tells_you_when_reporting_is_switched_off_and_asks_nothing(server, tmp_path):
    repo = init_repo(tmp_path, server, toml_extra="")
    (repo / "cckit.toml").write_text(f'[telemetry]\nenabled = false\nendpoint = "{server}/exec"\n')
    code, out = run_in_pty(["init", str(repo)], [])
    assert code == 0 and "Usage reporting to the team app is off ([telemetry] enabled = false" in out
    assert "Your company e-mail" not in out and identity.get_identity(server + "/exec") is None


@pytest.mark.skipif(not GIT, reason="needs git")
def test_init_does_not_ask_again_on_a_machine_that_is_already_registered(server, tmp_path):
    repo = init_repo(tmp_path, server)
    identity.save_identity(server + "/exec", ADA, "", "tok")
    code, out = run_in_pty(["init", str(repo)], [])
    assert code == 0 and "Your company e-mail" not in out and "not registered yet" not in out


@pytest.mark.skipif(not GIT, reason="needs git")
def test_init_with_the_key_already_in_the_toml_only_asks_for_the_email(server, tmp_path):
    repo = init_repo(tmp_path, server, with_key=True)
    code, out = run_in_pty(["init", str(repo)], [""])
    assert code == 0 and "Team key (from your admin)" not in out and "Save the team key?" not in out and f"Registered {ADA}" in out


@pytest.mark.skipif(not GIT, reason="needs git")
def test_ctrl_d_at_the_prompt_skips_registration_and_init_still_succeeds(server, tmp_path):
    repo = init_repo(tmp_path, server)
    code, out = run_in_pty(["init", str(repo)], ["\x04"])                      # end of input at the first prompt
    assert code == 0 and "Skipped" in out and (repo / ".claude/settings.json").exists()


def test_save_team_key_replaces_the_value_and_keeps_comments_and_other_text(tmp_path):
    (tmp_path / "cckit.toml").write_text('[model]\ndefault = "haiku"\n\n[telemetry]\nenabled = true\nteam_key = ""   # ask admin\nevents = ["auto"]\n')
    assert cli._save_team_key(tmp_path, "abcd1234") is True
    assert read(tmp_path / "cckit.toml") == '[model]\ndefault = "haiku"\n\n[telemetry]\nenabled = true\nteam_key = "abcd1234"   # ask admin\nevents = ["auto"]\n'


def test_save_team_key_adds_the_line_or_the_section_when_missing(tmp_path):
    (tmp_path / "cckit.toml").write_text('[telemetry]   # reporting\nenabled = true\n')
    cli._save_team_key(tmp_path, "abcd1234")
    assert config.load(tmp_path)["telemetry"]["team_key"] == "abcd1234" and "# reporting" in read(tmp_path / "cckit.toml")
    (tmp_path / "cckit.toml").write_text('[model]\ndefault = "haiku"')
    cli._save_team_key(tmp_path, "abcd1234")
    assert config.load(tmp_path)["telemetry"]["team_key"] == "abcd1234" and config.load(tmp_path)["model"]["default"] == "haiku"
    (tmp_path / "cckit.toml").unlink()
    cli._save_team_key(tmp_path, "abcd1234")                                      # no file at all
    assert config.load(tmp_path)["telemetry"]["team_key"] == "abcd1234"


@pytest.mark.parametrize("bad", ['x"; evil = "1', "a b c d", "ab", "k\nteam_key = 'y'", "é" * 8])
def test_save_team_key_refuses_text_that_could_break_the_file(tmp_path, bad):
    (tmp_path / "cckit.toml").write_text('[telemetry]\nteam_key = ""\n')
    assert cli._save_team_key(tmp_path, bad) is False
    assert read(tmp_path / "cckit.toml") == '[telemetry]\nteam_key = ""\n'


# ------------------------------------------------------------------ an existing CLAUDE.md
USER_MD = "# Our project\n\nWe use tabs. Run `make test` before pushing.\n\n## Deployment\nSee the wiki.\n"


def test_an_existing_claude_md_keeps_every_user_line_and_gets_one_managed_block(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(USER_MD)
    r = apply.init(tmp_path)
    md = read(tmp_path / "CLAUDE.md")
    assert md.startswith(USER_MD) and md.count("cckit:begin") == 1 and md.count("cckit:end") == 1
    assert any(l.startswith("merged") and "CLAUDE.md" in l and "your text is kept" in l for l in r.lines)
    again = apply.init(tmp_path)
    assert read(tmp_path / "CLAUDE.md") == md and any(l.startswith("unchanged") and "CLAUDE.md" in l for l in again.lines)


def test_text_the_user_adds_around_the_block_survives_later_updates(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(USER_MD)
    apply.init(tmp_path)
    md = read(tmp_path / "CLAUDE.md").replace("## Deployment", "## Deployment (edited)") + "\n## Added later\nKeep PRs small.\n"
    (tmp_path / "CLAUDE.md").write_text(md)
    (tmp_path / "cckit.toml").write_text("[usage]\nscripts = false\nlog = false\n")            # changes what the block says
    apply.init(tmp_path)
    new = read(tmp_path / "CLAUDE.md")
    assert "## Deployment (edited)" in new and "## Added later\nKeep PRs small.\n" in new and "usage_report.py" not in new
    assert new.count("cckit:begin") == 1


def test_a_windows_claude_md_keeps_its_line_endings(tmp_path):
    (tmp_path / "CLAUDE.md").write_bytes(USER_MD.replace("\n", "\r\n").encode())
    apply.init(tmp_path)
    raw = (tmp_path / "CLAUDE.md").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")                                  # no bare LF anywhere: the diff shows only the new block
    assert raw.startswith(USER_MD.replace("\n", "\r\n").encode())
    apply.init(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_bytes() == raw


@pytest.mark.parametrize("content", ["", "no trailing newline", "\n\n\n", "# Title\n\n\n"])
def test_odd_existing_claude_md_files(tmp_path, content):
    (tmp_path / "CLAUDE.md").write_text(content)
    apply.init(tmp_path)
    md = read(tmp_path / "CLAUDE.md")
    assert md.startswith(content) and md.count("cckit:begin") == 1 and md.endswith("<!-- cckit:end -->\n")


def test_claude_md_inside_dot_claude_is_used_when_there_is_no_root_one(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/CLAUDE.md").write_text(USER_MD)
    apply.init(tmp_path)
    assert not (tmp_path / "CLAUDE.md").exists()                                   # a second memory file would be loaded next to it
    assert read(tmp_path / ".claude/CLAUDE.md").startswith(USER_MD) and "cckit:begin" in read(tmp_path / ".claude/CLAUDE.md")
    assert not any(lv == "fail" for lv, _ in doctor.run(tmp_path, check_claude=False).items)


def test_when_both_exist_the_root_claude_md_gets_the_block(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/CLAUDE.md").write_text("# inner\n")
    (tmp_path / "CLAUDE.md").write_text(USER_MD)
    apply.init(tmp_path)
    assert "cckit:begin" in read(tmp_path / "CLAUDE.md") and read(tmp_path / ".claude/CLAUDE.md") == "# inner\n"


@pytest.mark.parametrize("broken", ["# mine\n<!-- cckit:begin - managed -->\nstray\n", "# mine\nstray\n<!-- cckit:end -->\n",
                                    "<!-- cckit:end -->\nuser text\n<!-- cckit:begin - managed -->\n"])
def test_a_lone_or_reversed_marker_stops_init_and_changes_nothing(tmp_path, broken, capsys):
    (tmp_path / "CLAUDE.md").write_text(broken)
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(ValueError, match="marker"):
        apply.init(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before and read(tmp_path / "CLAUDE.md") == broken      # nothing at all was written, not even cckit.toml
    assert cli.main(["init", str(tmp_path)]) == 2 and "marker" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_an_invalid_settings_json_stops_init_before_anything_is_written(tmp_path, capsys):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text("{ not json")
    (tmp_path / "CLAUDE.md").write_text(USER_MD)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert cli.main(["init", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err and "nothing was changed" in err
    assert sorted(p.name for p in (tmp_path / ".claude").iterdir()) == ["settings.json"] and read(tmp_path / "CLAUDE.md") == USER_MD
    assert not (tmp_path / "cckit.toml").exists() and sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_dry_run_writes_nothing_even_with_an_existing_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(USER_MD)
    before = sorted(p.name for p in tmp_path.iterdir())
    r = apply.init(tmp_path, dry_run=True)
    assert read(tmp_path / "CLAUDE.md") == USER_MD and sorted(p.name for p in tmp_path.iterdir()) == before
    assert any(l.startswith("merged") and "CLAUDE.md" in l for l in r.lines)


def test_a_claude_md_symlinked_to_agents_md_keeps_the_link(tmp_path):
    (tmp_path / "AGENTS.md").write_text(USER_MD)
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    apply.init(tmp_path)
    assert (tmp_path / "CLAUDE.md").is_symlink() and read(tmp_path / "AGENTS.md").startswith(USER_MD) and "cckit:begin" in read(tmp_path / "AGENTS.md")


def test_a_long_existing_claude_md_is_kept_and_doctor_says_to_trim_it(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("".join(f"rule {i}\n" for i in range(230)))
    apply.init(tmp_path)
    assert read(tmp_path / "CLAUDE.md").startswith("rule 0\n") and "rule 229\n" in read(tmp_path / "CLAUDE.md")
    assert any(lv == "fail" and "limit 200" in t for lv, t in doctor.run(tmp_path, check_claude=False).items)


def test_the_managed_block_stays_short_and_tells_claude_about_the_scripts_and_the_log(tmp_path):
    apply.init(tmp_path)
    md = read(tmp_path / "CLAUDE.md")
    assert len(md.splitlines()) < 30
    assert "usage_report.py" in md and "Do not estimate them yourself" in md and ".claude/token_usage.csv" in md


# ------------------------------------------------------------------ cckit login: save e-mail + token in the home folder, then init
def test_login_saves_the_identity_outside_the_repo_and_runs_init(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["login", str(repo), "--email", "Ada.Lovelace@TigerAnalytics.com", "--token", "tok_ABCDEF123456"]) == 0
    key = config.identity_key(config.load(repo)["telemetry"])
    assert identity.get_identity(key) == {"email": ADA, "github": "", "token": "tok_ABCDEF123456"}
    assert (repo / "cckit.toml").exists() and (repo / ".claude" / "settings.json").exists()       # init ran
    assert "tok_ABCDEF123456" not in "".join(p.read_text(errors="ignore") for p in repo.rglob("*") if p.is_file())   # the token is nowhere in the repo
    assert "not registered yet" not in capsys.readouterr().out                                    # init saw the login


def test_login_refuses_a_foreign_email_and_a_bad_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["login", str(tmp_path), "--email", "ada@gmail.com", "--token", "tok_ABCDEF123456"]) == 2
    assert "@tigeranalytics.com" in capsys.readouterr().err
    assert cli.main(["login", str(tmp_path), "--email", ADA, "--token", "short"]) == 2
    assert "token" in capsys.readouterr().err
    assert not (tmp_path / "cckit.toml").exists() and not identity.identity_path().exists()      # nothing written on a refusal


def test_login_prompts_with_hidden_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": ADA)
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": asked.append(prompt) or "tok_ABCDEF123456")
    assert cli.main(["login", str(tmp_path), "--no-init"]) == 0
    assert asked and not (tmp_path / "cckit.toml").exists()                                       # --no-init: login only
    assert identity.get_identity(config.identity_key(config.load(tmp_path)["telemetry"]))["token"] == "tok_ABCDEF123456"


# ------------------------------------------------------------------ the Stop hook reports usage (trigger "stop")
def msg_line(mid, model="claude-sonnet-5-5", inp=10, out=50, cr=100, cw=20, side=False, sid="sess-1"):
    return json.dumps({"type": "assistant", "uuid": "u" + mid, "isSidechain": side, "timestamp": "2026-07-01T10:00:00Z", "sessionId": sid,
                       "message": {"id": mid, "model": model, "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                                                                         "cache_creation_input_tokens": cw},
                                   "content": [{"type": "text", "text": "SECRET ANSWER"}]}})


def stop_hook(repo, tr, extra_env=None):
    payload = {"session_id": "sess-1", "hook_event_name": "Stop", "cwd": str(repo), "transcript_path": str(tr)}
    p = subprocess.run([sys.executable, str(repo / ".claude/hooks/log_token_usage.py")], input=json.dumps(payload), capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), **(extra_env or {})}, timeout=60)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""
    return p


def stop_state(repo):
    return json.loads((repo / ".claude/token_usage.csv.state.json").read_text())


def age_state(repo, **kv):
    p = repo / ".claude/token_usage.csv.state.json"
    d = json.loads(p.read_text())
    d.update(kv)
    p.write_text(json.dumps(d))


def stop_events(server):
    return [r for r in rows(server, "Claude_Compactions") if r["Trigger"] == "stop"]


def test_stop_hook_posts_per_model_totals_once(server, tmp_path):
    setup(server, tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join([msg_line("m1", inp=10, out=50, cr=100, cw=20), msg_line("m1", inp=10, out=60, cr=100, cw=20),   # a message is written once per content block
                             msg_line("m2", inp=5, out=40, cr=300, cw=0), msg_line("h1", model="claude-haiku-5-5", inp=1, out=7, cr=0, cw=0, side=True)]) + "\n")
    stop_hook(tmp_path, tr)
    ev = {r["Model"]: r for r in stop_events(server)}
    assert set(ev) == {"claude-sonnet-5-5", "claude-haiku-5-5"}                       # one event per model, so each is priced with its own rates
    s = ev["claude-sonnet-5-5"]
    assert (s["Input Tokens"], s["Output Tokens"], s["Cache Read"], s["Cache Write"], s["Requests"]) == (15, 100, 400, 20, 2)   # the final count of m1 (60), not both lines
    assert (s["Context Tokens"], s["Session"], s["Project"], s["Client"], s["Email"]) == (305, "sess-1", tmp_path.name, "cckit-stop/2", ADA)
    h = ev["claude-haiku-5-5"]
    assert (h["Input Tokens"], h["Output Tokens"], h["Requests"], h["Subagent Requests"], h["Subagent Output"]) == (1, 7, 0, 1, 7)
    assert rows(server, "Claude_Users")[0]["Compactions"] == 0                         # Stop events are not compactions
    stop_hook(tmp_path, tr)                                                           # nothing new: nothing sent
    assert len(stop_events(server)) == 2
    whole = json.dumps(dump(server)) + (tmp_path / ".claude/token_usage.csv.state.json").read_text()
    tok = identity.get_identity(server + "/exec")["token"]
    assert "SECRET" not in whole and tok not in whole                                 # no text from the conversation, and the token is not kept in the state file


def test_stop_reports_wait_a_minute_and_are_not_lost(server, tmp_path):
    setup(server, tmp_path, extra="stop_interval = 60\n")
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr = tmp_path / "t.jsonl"
    tr.write_text(msg_line("m1") + "\n")
    stop_hook(tmp_path, tr)
    assert len(stop_events(server)) == 1
    tr.write_text(tr.read_text() + msg_line("m2", out=70) + "\n")
    stop_hook(tmp_path, tr)                                                           # less than a minute later: kept, not sent
    assert len(stop_events(server)) == 1 and stop_state(tmp_path)["pending"]["claude-sonnet-5-5"]["o"] == 70
    age_state(tmp_path, last_flush=time.time() - 120)
    stop_hook(tmp_path, tr)
    ev = stop_events(server)
    assert len(ev) == 2 and sorted(r["Output Tokens"] for r in ev) == [50, 70]         # the waiting numbers went out with the next event
    assert stop_state(tmp_path)["pending"] == {} and stop_state(tmp_path)["outbox"] == []


def test_undelivered_events_are_retried_with_the_same_id(server, tmp_path):
    setup(server, tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    good = identity.get_identity(server + "/exec")
    identity.save_identity(server + "/exec", ADA, "", "wrong-token-value")
    tr = tmp_path / "t.jsonl"
    tr.write_text(msg_line("m1") + "\n")
    stop_hook(tmp_path, tr)
    st = stop_state(tmp_path)
    assert stop_events(server) == [] and len(st["outbox"]) == 1 and st["next_try"] > time.time() and st["fails"] == 1    # refused: kept, with a pause
    eid = st["outbox"][0]["event_id"]
    identity.save_identity(server + "/exec", ADA, "", good["token"])
    stop_hook(tmp_path, tr)
    assert stop_events(server) == [] and len(stop_state(tmp_path)["outbox"]) == 1                                         # still inside the pause
    age_state(tmp_path, next_try=0)
    stop_hook(tmp_path, tr)
    ev = stop_events(server)
    assert len(ev) == 1 and stop_state(tmp_path)["outbox"] == [] and stop_state(tmp_path)["fails"] == 0
    assert APIS[server].export("events", 0, 10)["rows"][0][1] == eid                                                       # the id that was kept is the id that arrived


def test_nothing_is_collected_before_registration_or_when_switched_off(server, tmp_path):
    setup(server, tmp_path)
    tr = tmp_path / "t.jsonl"
    tr.write_text(msg_line("m1") + "\n")
    stop_hook(tmp_path, tr)                                                           # not registered
    assert stop_events(server) == [] and "pending" not in stop_state(tmp_path) and "outbox" not in stop_state(tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr.write_text(tr.read_text() + msg_line("m2") + "\n")
    stop_hook(tmp_path, tr, {"CCKIT_TELEMETRY": "off"})
    assert stop_events(server) == [] and (tmp_path / ".claude/token_usage.csv").exists()   # the CSV is still written locally


def test_stop_report_can_be_turned_off_and_the_hook_stays_for_the_csv(server, tmp_path):
    setup(server, tmp_path, extra="stop_report = false\n")
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr = tmp_path / "t.jsonl"
    tr.write_text(msg_line("m1") + "\n")
    stop_hook(tmp_path, tr)
    assert stop_events(server) == [] and len(read(tmp_path / ".claude/token_usage.csv").splitlines()) == 2


def test_stop_hook_without_the_csv_still_reports(server, tmp_path):
    setup(server, tmp_path)
    cfg_text = read(tmp_path / "cckit.toml") + "\n[usage]\nlog = false\n"
    (tmp_path / "cckit.toml").write_text(cfg_text)
    apply.init(tmp_path)
    s = json.loads(read(tmp_path / ".claude/settings.json"))
    assert "Stop" in s["hooks"] and "Read(.claude/token_usage.csv)" not in s["permissions"]["deny"]
    assert "/.claude/token_usage.csv.state.json" in read(tmp_path / ".gitignore")
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr = tmp_path / "t.jsonl"
    tr.write_text(msg_line("m1") + "\n")
    stop_hook(tmp_path, tr)
    assert len(stop_events(server)) == 1 and not (tmp_path / ".claude/token_usage.csv").exists()


def test_stop_report_validation():
    cfg = config.defaults()
    cfg["telemetry"]["stop_report"] = "yes"
    assert any("stop_report" in e for e in config.validate(cfg))


def test_turns_count_the_stop_hook_firings_once_per_batch(server, tmp_path):
    setup(server, tmp_path, extra="stop_interval = 60\n")
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join([msg_line("m1", out=50), msg_line("h1", model="claude-haiku-5-5", out=7, side=True)]) + "\n")
    stop_hook(tmp_path, tr)                                                            # turn 1: sent at once, on the model that wrote the most
    ev = {r["Model"]: r for r in stop_events(server)}
    assert (ev["claude-sonnet-5-5"]["Turns"], ev["claude-haiku-5-5"]["Turns"]) == (1, 0)
    stop_hook(tmp_path, tr)                                                            # turn 2, 3: nothing new and inside the minute: counted, kept
    stop_hook(tmp_path, tr)
    assert stop_state(tmp_path)["stops"] == 2 and len(stop_events(server)) == 2
    sub = {"session_id": "sess-1", "hook_event_name": "SubagentStop", "cwd": str(tmp_path), "agent_transcript_path": str(tr)}
    subprocess.run([sys.executable, str(tmp_path / ".claude/hooks/log_token_usage.py")], input=json.dumps(sub), capture_output=True, text=True,
                   env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)}, timeout=30)
    assert stop_state(tmp_path)["stops"] == 2                                          # a subagent finishing is not a turn
    tr.write_text(tr.read_text() + msg_line("m2", out=70) + "\n")
    age_state(tmp_path, last_flush=time.time() - 120)
    stop_hook(tmp_path, tr)                                                            # turn 4: the batch says 3 turns (2, 3 and 4)
    ev = stop_events(server)
    assert sorted(r["Turns"] for r in ev if r["Turns"]) == [1, 3] and sum(r["Turns"] for r in ev) == 4      # 4 firings in all, none counted twice
    assert stop_state(tmp_path)["stops"] == 0


def test_an_older_database_gets_the_turns_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(usage_server.SCHEMA.replace(", turns INTEGER", ""))
    c.commit()
    c.close()
    api = usage_server.UsageApi(str(db), KEY, [DOM], admin_token=ADMIN, github_check=fake_github)
    assert "Turns" in api.export("events")["columns"]


def turn_lines(prefix, t0, model="claude-sonnet-5-5", effort="high", tools=("Bash", "Read", "Bash"), sub_model=None):
    """A typed prompt at t0, then tool calls and an answer, the last line 95 seconds later."""
    def ts(sec):
        return f"2026-07-01T10:{(sec // 60):02d}:{(sec % 60):02d}Z"
    out = [json.dumps({"type": "user", "uuid": prefix + "u", "timestamp": ts(t0), "sessionId": "sess-1", "message": {"role": "user", "content": "SECRET PROMPT"}})]
    for i, tool in enumerate(tools):
        out.append(json.dumps({"type": "assistant", "uuid": f"{prefix}a{i}", "timestamp": ts(t0 + 10 * (i + 1)), "sessionId": "sess-1", "effort": effort,
                               "version": "2.1.400", "entrypoint": "cli", "gitBranch": "feature/x",
                               "message": {"id": f"{prefix}m{i}", "model": model, "usage": {"input_tokens": 1, "output_tokens": 10, "cache_read_input_tokens": 100,
                                                                                          "cache_creation_input_tokens": 0},
                                           "content": [{"type": "tool_use", "id": f"{prefix}t{i}", "name": tool, "input": {"command": "SECRET"}}]}}))
        out.append(json.dumps({"type": "user", "uuid": f"{prefix}r{i}", "timestamp": ts(t0 + 10 * (i + 1) + 1), "sessionId": "sess-1",
                               "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"{prefix}t{i}", "content": "SECRET"}]}}))
    out.append(msg_line(prefix + "fin", model=model, out=20).replace("2026-07-01T10:00:00Z", ts(t0 + 95)))
    if sub_model:
        out.append(msg_line(prefix + "sub", model=sub_model, out=5, side=True).replace("2026-07-01T10:00:00Z", ts(t0 + 50)))
    return out


def test_each_turn_reports_current_model_effort_tools_and_duration(server, tmp_path):
    setup(server, tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(turn_lines("a", 0, model="claude-opus-5-5", effort="max", sub_model="claude-haiku-5-5")) + "\n")
    stop_hook(tmp_path, tr)
    ev = {r["Model"]: r for r in stop_events(server)}
    assert set(ev) == {"claude-opus-5-5", "claude-haiku-5-5"}
    o, h = ev["claude-opus-5-5"], ev["claude-haiku-5-5"]
    assert (o["Turns"], o["Duration (s)"], o["Tool Calls"], o["Tools"]) == (1, 95, 3, "Bash:2;Read:1")
    assert (h["Turns"], h["Duration (s)"], h["Tool Calls"], h["Tools"]) == (0, "", 0, "")                         # counted once per turn, on the main model's event
    for r in (o, h):
        assert (r["Current Model"], r["Effort"], r["Claude Version"], r["Entrypoint"], r["Branch"]) == ("claude-opus-5-5", "max", "2.1.400", "cli", "feature/x")
    assert o["Output Tokens"] == 50 and h["Output Tokens"] == 5                                                   # model-level usage stays separate
    assert o["Session"] == h["Session"] == "sess-1" and o["Session Minutes"] == 1                                 # 95 s since the session began
    tr.write_text(tr.read_text() + "\n".join(turn_lines("b", 300, model="claude-sonnet-5-5", effort="low", tools=("Edit",))) + "\n")
    stop_hook(tmp_path, tr)                                                                                       # the second turn: only its own prompt, tools and time
    s = [r for r in stop_events(server) if r["Model"] == "claude-sonnet-5-5"][0]
    assert (s["Turns"], s["Duration (s)"], s["Tool Calls"], s["Tools"], s["Current Model"], s["Effort"]) == (1, 95, 1, "Edit:1", "claude-sonnet-5-5", "low")
    assert s["Session Minutes"] == 6                                                                              # a running total: 395 s since the first line
    whole = json.dumps(dump(server)) + (tmp_path / ".claude/token_usage.csv.state.json").read_text()
    assert "SECRET" not in whole                                                                                  # prompts and tool inputs never leave the machine


def test_branch_is_not_sent_without_send_project(server, tmp_path):
    setup(server, tmp_path, extra="send_project = false\n")
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(turn_lines("a", 0)) + "\n")
    stop_hook(tmp_path, tr)
    r = stop_events(server)[0]
    assert r["Branch"] == "" and r["Project"] == "" and r["Effort"] == "high"


def test_stop_interval_validation():
    cfg = config.defaults()
    cfg["telemetry"]["stop_interval"] = -1
    assert any("stop_interval" in e for e in config.validate(cfg))
    cfg["telemetry"]["stop_interval"] = "5"
    assert any("stop_interval" in e for e in config.validate(cfg))


def test_unknown_duration_is_left_empty_not_zero(server, tmp_path):
    setup(server, tmp_path)
    cli.main(["register", str(tmp_path), "--email", ADA])
    tr = tmp_path / "t.jsonl"
    tr.write_text(msg_line("m1") + "\n")                    # no typed prompt in the transcript: the start of the turn is unknown
    stop_hook(tmp_path, tr)
    r = stop_events(server)[0]
    assert r["Turns"] == 1 and r["Duration (s)"] == ""
