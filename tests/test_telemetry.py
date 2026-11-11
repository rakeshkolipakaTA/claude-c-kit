"""Usage reporting: config, generated hook, the two APIs (register, usage), Tiger e-mail checks, and end-to-end runs.

The end-to-end tests run the REAL Apps Script files that ship in the package (src/cckit/assets/apps_script) in Node behind a small HTTP
server that behaves like a deployed web app (see tests/sim). They are skipped when `node` is not installed.
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

SIM = Path(__file__).parent / "sim" / "usage_server_sim.js"
GS_DIR = Path(render.__file__).parent / "assets" / "apps_script"
KEY = "teamkey123"
ADMIN = "admintoken456"
DOM = "tigeranalytics.com"
ADA = "ada.lovelace@tigeranalytics.com"
GIT = shutil.which("git")
NODE = shutil.which("node")     # resolved now: the fixture below narrows PATH
REAL_PATH = os.environ.get("PATH", "")


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
def test_telemetry_off_by_default_writes_nothing(tmp_path):
    apply.init(tmp_path)
    s = json.loads(read(tmp_path / ".claude/settings.json"))
    assert "PreCompact" not in s["hooks"] and "SessionStart" not in s["hooks"]
    assert not (tmp_path / ".claude/hooks/compact_report.py").exists()


def test_enabled_without_endpoint_warns_and_writes_no_hook(tmp_path):
    (tmp_path / "cckit.toml").write_text("[telemetry]\nenabled = true\n")
    apply.init(tmp_path)
    assert "PreCompact" not in json.loads(read(tmp_path / ".claude/settings.json"))["hooks"]
    rep = doctor.run(tmp_path, check_claude=False)
    assert any(lv == "warn" and "endpoint is empty" in t for lv, t in rep.items)


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


# ------------------------------------------------------------------ the two APIs, end to end against the real Apps Script files
BACKENDS = {}      # base URL -> (kind, UsageApi or None): lets the helpers below read either backend the same way


def fake_github(login):
    return {"nosuchuser": "No", "ratelimited": "Unchecked"}.get(login, "Yes")


@pytest.fixture(params=["appscript", "python"])
def server(request, tmp_path_factory):
    """The same API twice: the Google Apps Script files (run in Node) and `cckit server` (Python). Every test using this fixture must pass on both."""
    if request.param == "appscript":
        if not NODE:
            pytest.skip("needs node")
        proc = subprocess.Popen([NODE, str(SIM), str(GS_DIR), KEY], stdout=subprocess.PIPE, text=True)
        base = f"http://127.0.0.1:{int(proc.stdout.readline().split()[1])}"
        BACKENDS[base] = ("appscript", None)
        yield base
        proc.terminate()
        return
    import threading
    api = usage_server.UsageApi(str(tmp_path_factory.mktemp("db") / "usage.db"), KEY, [DOM], admin_token=ADMIN, github_check=fake_github)
    srv = usage_server.make_server("127.0.0.1", 0, api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    BACKENDS[base] = ("python", api)
    yield base
    srv.shutdown()
    srv.server_close()


def dump(base):
    kind, api = BACKENDS[base]
    if kind == "appscript":
        return json.loads(urllib.request.urlopen(base + "/_dump").read())
    out = {}
    u, e = api.export("users"), api.export("events", 0, 2000)
    if u["rows"]:
        out["Claude_Users"] = [u["columns"]] + u["rows"]
    if e["rows"]:
        out["Claude_Compactions"] = [e["columns"]] + e["rows"]
    return out


def rows(base, tab):
    d = dump(base).get(tab) or [[]]
    return [dict(zip(d[0], r)) for r in d[1:]]


def clear_tokens(base, email):
    """What an admin does: Apps Script = clear the 'Token Hashes' cell in the sheet; server = `cckit server reset`."""
    kind, api = BACKENDS[base]
    if kind == "appscript":
        urllib.request.urlopen(base + f"/_clearhash?u={email}").read()
    else:
        assert api.reset(email)


def devices(base, email):
    u = [r for r in rows(base, "Claude_Users") if r["Email"] == email][0]
    return int(u["Devices"]) if "Devices" in u else len([h for h in u["Token Hashes"].split(",") if h])


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
def test_api_export_writes_the_three_files_and_check_notices_drift(tmp_path, capsys):
    out = tmp_path / "gs"
    assert cli.main(["api", "export", "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["Api.gs", "Register.gs", "Usage.gs"]
    assert cli.main(["api", "export", "--check", "--out", str(out)]) == 0
    (out / "Usage.gs").write_text("// edited by hand\n")
    assert cli.main(["api", "export", "--check", "--out", str(out)]) == 1
    assert "Usage.gs" in capsys.readouterr().out
    assert cli.main(["api", "export", "--standalone", "--out", str(out)]) == 0
    assert (out / "Standalone.gs").exists() and "edited by hand" not in read(out / "Usage.gs")


@pytest.mark.skipif(not NODE, reason="needs node")
def test_exported_apps_script_files_parse(tmp_path):
    out = tmp_path / "gs"
    cli.main(["api", "export", "--standalone", "--out", str(out)])
    for f in out.glob("*.gs"):
        js = tmp_path / (f.stem + ".js")
        js.write_text(read(f))
        assert subprocess.run([NODE, "--check", str(js)], capture_output=True, text=True).returncode == 0, f.name


def test_api_files_never_reference_conversation_content():
    src = "".join(read(p) for p in GS_DIR.glob("*.gs"))
    for word in ("compact_summary", "custom_instructions", "prompt"):
        assert word not in src.replace("prompts, code", "").replace("ui.prompt", "")


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
    with pytest.raises(identity.RegisterError, match="Google sign-in.*Who has access: Anyone"):
        identity.ping(login_page_server)
    write_cfg(tmp_path, endpoint=login_page_server)
    assert cli.main(["whoami", str(tmp_path), "--check"]) == 1
    assert "Google sign-in" in capsys.readouterr().out
    assert cli.main(["register", str(tmp_path), "--email", ADA]) == 1
    assert "Google sign-in" in capsys.readouterr().err


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
def pyserver(tmp_path):
    import threading
    api = new_api(tmp_path)
    srv = usage_server.make_server("127.0.0.1", 0, api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", api
    srv.shutdown()
    srv.server_close()


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


# ------------------------------------------------------------------ Pull.gs: the sheet copies rows from the server (Node + a real server)
PULL_SIM = Path(__file__).parent / "sim" / "puller_sim.js"


def pull(tmp_path, base, action="pull", token=ADMIN, answers=None, state="sheet", rewrite=None):
    args = [NODE, str(PULL_SIM), str(GS_DIR), str(tmp_path / f"{state}_state.json"), base, token, action] + ([json.dumps(answers)] if answers is not None else [])
    env = {**os.environ, "PATH": REAL_PATH, **({"PULL_SIM_REWRITE": rewrite} if rewrite else {})}      # the sim shells out to curl
    p = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="needs node")
def test_pull_copies_new_rows_once_and_only_new_rows(pyserver, tmp_path):
    base, api = pyserver
    tok = seed(base, 3)
    identity.post_json(base + "/?api=usage", {"email": ADA, "token": tok, "event_id": "fx", "model": "=SUM(A1)", "project": "+cmd"})
    r = pull(tmp_path, base)
    assert r.get("error") is None and r["result"] == {"events": 4, "users": 1}
    ev = r["sheets"]["Claude_Compactions"]
    assert ev[0][:3] == ["Received At", "Event ID", "Email"] and [row[1] for row in ev[1:]] == ["ev0", "ev1", "ev2", "fx"]
    assert ev[4][5] == "'=SUM(A1)" and ev[4][25] == "'+cmd"                                # formulas neutralised
    assert r["sheets"]["Claude_Users"][1][0] == ADA and r["props"]["USAGE_PULL_AFTER"] == "4"
    assert pull(tmp_path, base)["result"] == {"events": 0, "users": 1}                   # nothing new: nothing copied twice
    identity.post_json(base + "/?api=usage", {"email": ADA, "token": tok, "event_id": "ev9"})
    r = pull(tmp_path, base)
    assert r["result"]["events"] == 1 and [row[1] for row in r["sheets"]["Claude_Compactions"][1:]] == ["ev0", "ev1", "ev2", "fx", "ev9"]
    assert len(r["sheets"]["Claude_Users"]) == 2                                          # header + one developer: replaced, not appended
    assert tok not in json.dumps(r["sheets"]) and sha_of(tok) not in json.dumps(r["sheets"])


@pytest.mark.skipif(not NODE, reason="needs node")
def test_pull_can_start_over_without_duplicating(pyserver, tmp_path):
    base, _ = pyserver
    seed(base, 2)
    assert pull(tmp_path, base)["result"]["events"] == 2
    again = pull(tmp_path, base, "scratch")
    assert again["result"]["events"] == 2 and [row[1] for row in again["sheets"]["Claude_Compactions"][1:]] == ["ev0", "ev1"]


@pytest.mark.skipif(not NODE, reason="needs node")
def test_pull_says_what_is_wrong_when_the_token_is_wrong_or_export_is_off(pyserver, tmp_path):
    base, _ = pyserver
    assert "rejected the admin token" in pull(tmp_path, base, token="wrong")["error"]
    import threading
    srv = usage_server.make_server("127.0.0.1", 0, usage_server.UsageApi(str(tmp_path / "n.db"), KEY, [DOM], admin_token=""))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert "not enabled" in pull(tmp_path, f"http://127.0.0.1:{srv.server_address[1]}", token="x", state="noexport")["error"]
    finally:
        srv.shutdown()
    assert "not set up" in pull(tmp_path, "", token="", state="unset")["error"]


@pytest.mark.skipif(not NODE, reason="needs node")
def test_setup_refuses_http_can_be_cancelled_and_installs_exactly_one_trigger(pyserver, tmp_path):
    base, _ = pyserver
    seed(base, 2)
    rw = f"https://usage.test|{base}"
    clean = lambda state: {"state": state, "rewrite": rw}
    # the setup flow must not rely on pre-set properties: run it from an empty state
    bad = pull(tmp_path, "", token="", action="setup", answers=["http://insecure.example.com", ADMIN], **clean("a"))
    assert any("must start with https://" in m for m in bad["alerts"]) and bad["triggers"] == [] and "USAGE_ADMIN_TOKEN" not in bad["props"]
    cancel = pull(tmp_path, "", token="", action="setup", answers=[None], **clean("b"))
    assert cancel["triggers"] == [] and cancel["props"] == {}
    ok = pull(tmp_path, "", token="", action="setup", answers=["https://usage.test/?x=1#y", ADMIN], **clean("c"))
    assert ok.get("error") is None and ok["props"]["USAGE_SERVER_URL"] == "https://usage.test" and ok["props"]["USAGE_ADMIN_TOKEN"] == ADMIN
    assert ok["triggers"] == [{"fn": "pullClaudeUsage", "every": 15}] and "2 compactions, 1 developers" in ok["alerts"][-1]
    twice = pull(tmp_path, "", token="", action="setup", answers=["https://usage.test", ADMIN], **clean("c"))
    assert twice["triggers"] == [{"fn": "pullClaudeUsage", "every": 15}]                   # replaced, not duplicated
    assert len(twice["sheets"]["Claude_Compactions"]) == 3 and twice["props"]["USAGE_PULL_AFTER"] == "2"   # same server: cursor kept, header + 2 rows, no repeats
    assert "1 compactions" not in twice["alerts"][-1] and "0 compactions" in twice["alerts"][-1]


# ------------------------------------------------------------------ api export modes
def test_api_export_puller_mode_writes_only_pull_gs(tmp_path):
    out = tmp_path / "gs"
    assert cli.main(["api", "export", "--mode", "puller", "--out", str(out)]) == 0
    assert [p.name for p in out.iterdir()] == ["Pull.gs"]
    assert cli.main(["api", "export", "--mode", "puller", "--check", "--out", str(out)]) == 0
    (out / "Pull.gs").write_text("//x")
    assert cli.main(["api", "export", "--mode", "puller", "--check", "--out", str(out)]) == 1


@pytest.mark.skipif(not NODE, reason="needs node")
def test_pull_gs_parses_and_has_no_web_entry_point(tmp_path):
    js = tmp_path / "p.js"
    js.write_text(read(GS_DIR / "Pull.gs"))
    assert subprocess.run([NODE, "--check", str(js)], capture_output=True).returncode == 0
    assert "doPost" not in read(GS_DIR / "Pull.gs") and "doGet" not in read(GS_DIR / "Pull.gs")      # nothing public is exposed
