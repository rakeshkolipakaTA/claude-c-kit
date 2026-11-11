"""Local usage log (Stop hook -> CSV) and the scripts Claude can run for session usage and cost."""
import csv
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cckit import apply, cli, config, doctor, render, report


def read(p):
    return Path(p).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A private ~/.claude, so the real transcripts are never read and nothing is written outside the test."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CCKIT_USAGE_LOG", raising=False)
    return tmp_path / "claude_home"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    return r


def A(mid, model="claude-sonnet-5-5", inp=10, out=50, cr=100, cw=20, ts="2026-07-01T10:00:00Z", side=False, tools=(), effort="high", sid="sess-1"):
    msg = {"id": mid, "model": model, "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw},
           "content": [{"type": "tool_use", "id": f"{mid}-{t}", "name": t, "input": {"cmd": "SECRET"}} for t in tools] + [{"type": "text", "text": "SECRET ANSWER"}]}
    return json.dumps({"type": "assistant", "uuid": "u" + mid, "isSidechain": side, "timestamp": ts, "effort": effort, "version": "2.1.300",
                       "entrypoint": "cli", "gitBranch": "main", "sessionId": sid, "message": msg})


def write(path, lines):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def hook(repo, tpath, event="Stop", sid="sess-1", extra_env=None, key="transcript_path"):
    payload = {"session_id": sid, "hook_event_name": event, "cwd": str(repo), key: str(tpath)}
    p = subprocess.run([sys.executable, str(repo / ".claude/hooks/log_token_usage.py")], input=json.dumps(payload), capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), **(extra_env or {})}, timeout=30)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""            # silent: a Stop hook must not talk
    return p


def rows(repo, rel=".claude/token_usage.csv"):
    f = repo / rel
    return list(csv.DictReader(f.open(encoding="utf-8"))) if f.exists() else []


# ------------------------------------------------------------------ settings, files, gitignore
def test_defaults_install_the_log_hook_scripts_command_and_allow_rule(repo):
    apply.init(repo)
    s = json.loads(read(repo / ".claude/settings.json"))
    for ev in ("Stop", "SubagentStop"):
        h = s["hooks"][ev][0]["hooks"][0]
        assert h["args"] == ["${CLAUDE_PROJECT_DIR}/.claude/hooks/log_token_usage.py"] and "async" not in h      # synchronous: headless runs would kill a background hook
    assert "Bash(python3 .claude/scripts/usage_report.py:*)" in s["permissions"]["allow"]
    assert (repo / ".claude/scripts/usage_report.py").exists() and (repo / ".claude/commands/usage.md").exists()
    assert "usage_report.py" in read(repo / "CLAUDE.md") and "Do not estimate them yourself" in read(repo / "CLAUDE.md")


def test_csv_and_state_are_gitignored_and_user_lines_survive(repo):
    (repo / ".gitignore").write_text("node_modules/\n.env\n")
    apply.init(repo)
    gi = read(repo / ".gitignore")
    assert gi.startswith("node_modules/\n.env\n") and "/.claude/token_usage.csv\n" in gi and "/.claude/token_usage.csv.state.json\n" in gi
    apply.init(repo)
    assert read(repo / ".gitignore") == gi                                          # idempotent: one block, not two
    assert gi.count("cckit:begin") == 1


def test_gitignore_is_created_when_missing_and_block_removed_when_logging_is_turned_off(repo):
    apply.init(repo)
    assert (repo / ".gitignore").exists()
    (repo / "cckit.toml").write_text("[usage]\nlog = false\n")
    apply.init(repo)
    assert not (repo / ".gitignore").exists()                                       # only our block was in it: the file we created goes away
    assert not (repo / ".claude/hooks/log_token_usage.py").exists()
    s = json.loads(read(repo / ".claude/settings.json"))
    assert "Stop" not in s["hooks"] and "SubagentStop" not in s["hooks"]


def test_turning_the_log_off_keeps_the_users_own_gitignore_lines(repo):
    (repo / ".gitignore").write_text("dist/\n")
    apply.init(repo)
    (repo / "cckit.toml").write_text("[usage]\nlog = false\n")
    apply.init(repo)
    assert read(repo / ".gitignore").strip() == "dist/"


def test_custom_csv_path_is_used_everywhere(repo):
    (repo / "cckit.toml").write_text('[usage]\ncsv_path = "logs/tokens.csv"\n')
    apply.init(repo)
    assert 'CSV_PATH = "logs/tokens.csv"' in read(repo / ".claude/hooks/log_token_usage.py")
    assert "/logs/tokens.csv\n/logs/tokens.csv.state.json" in read(repo / ".gitignore")


@pytest.mark.parametrize("bad", ["/etc/x.csv", "../x.csv", "a/../../x.csv", "C:\\x.csv", "", "\\\\server\\share\\x.csv"])
def test_csv_path_must_stay_inside_the_repo(bad):
    cfg = config.defaults()
    cfg["usage"]["csv_path"] = bad
    assert any("csv_path" in e for e in config.validate(cfg)), bad


def test_users_own_allow_rules_survive_and_ours_is_taken_back(repo):
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(npm test)"]}}))
    apply.init(repo)
    assert json.loads(read(repo / ".claude/settings.json"))["permissions"]["allow"] == ["Bash(npm test)", "Bash(python3 .claude/scripts/usage_report.py:*)"]
    (repo / "cckit.toml").write_text("[usage]\nscripts = false\n")
    apply.init(repo)
    assert json.loads(read(repo / ".claude/settings.json"))["permissions"]["allow"] == ["Bash(npm test)"]


def test_doctor_reports_the_log_and_flags_a_missing_gitignore_block(repo):
    apply.init(repo)
    assert any(lv == "ok" and "git-ignored" in t for lv, t in doctor.run(repo, check_claude=False).items)
    (repo / ".gitignore").unlink()
    assert any(lv == "warn" and ".gitignore" in t for lv, t in doctor.run(repo, check_claude=False).items)


# ------------------------------------------------------------------ the Stop hook
def test_stop_hook_logs_new_responses_once_with_final_counts(repo, tmp_path):
    apply.init(repo)
    tr = write(tmp_path / "t.jsonl", [
        json.dumps({"type": "user", "message": {"role": "user", "content": "SECRET PROMPT"}}),
        A("m1", inp=5, out=1), A("m1", inp=10, out=50),                              # one message written twice: the last line is final
        A("m2", model="claude-haiku-5-5", inp=3, out=7, cr=0, cw=0, side=True),
        A("syn", model="<synthetic>"),
        "not json"])
    hook(repo, tr)
    r = rows(repo)
    assert [x["message_id"] for x in r] == ["m1", "m2"]
    assert (r[0]["input_tokens"], r[0]["output_tokens"], r[0]["cache_write_tokens"], r[0]["cache_read_tokens"], r[0]["subagent"]) == ("10", "50", "20", "100", "no")
    assert r[1]["subagent"] == "yes" and r[1]["model"] == "claude-haiku-5-5" and r[0]["session_id"] == "sess-1"
    assert list(r[0]) == ["timestamp", "session_id", "message_id", "model", "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "subagent"]
    assert "SECRET" not in read(repo / ".claude/token_usage.csv")
    hook(repo, tr)
    hook(repo, tr)
    assert len(rows(repo)) == 2                                                     # repeated Stop events add nothing


def test_stop_hook_appends_only_what_is_new(repo, tmp_path):
    apply.init(repo)
    tr = write(tmp_path / "t.jsonl", [A("m1")])
    hook(repo, tr)
    with open(tr, "a", encoding="utf-8") as f:
        f.write(A("m2", ts="2026-07-01T10:05:00Z") + "\n")
    hook(repo, tr)
    assert [x["message_id"] for x in rows(repo)] == ["m1", "m2"]


def test_stop_hook_survives_a_lost_state_file_without_duplicates(repo, tmp_path):
    apply.init(repo)
    tr = write(tmp_path / "t.jsonl", [A("m1"), A("m2")])
    hook(repo, tr)
    (repo / ".claude/token_usage.csv.state.json").unlink()
    hook(repo, tr)
    assert [x["message_id"] for x in rows(repo)] == ["m1", "m2"]


def test_stop_hook_ignores_a_half_written_last_line_until_it_is_complete(repo, tmp_path):
    apply.init(repo)
    tr = tmp_path / "t.jsonl"
    tr.write_text(A("m1") + "\n" + A("m2")[:60], encoding="utf-8")                  # the writer is mid-line
    hook(repo, tr)
    assert [x["message_id"] for x in rows(repo)] == ["m1"]
    tr.write_text(A("m1") + "\n" + A("m2") + "\n", encoding="utf-8")
    hook(repo, tr)
    assert [x["message_id"] for x in rows(repo)] == ["m1", "m2"]


def test_stop_hook_handles_a_truncated_transcript(repo, tmp_path):
    apply.init(repo)
    tr = write(tmp_path / "t.jsonl", [A("m1"), A("m2"), A("m3")])
    hook(repo, tr)
    write(tr, [A("m4")])                                                           # shorter than the stored offset: start over, no crash
    hook(repo, tr)
    assert [x["message_id"] for x in rows(repo)] == ["m1", "m2", "m3", "m4"]


def test_subagent_stop_reads_the_subagent_transcript(repo, tmp_path):
    apply.init(repo)
    main = write(tmp_path / "main.jsonl", [A("m1")])
    sub = write(tmp_path / "main" / "subagents" / "agent-1.jsonl", [A("s1", model="claude-haiku-5-5", out=9)])
    hook(repo, main)
    hook(repo, sub, event="SubagentStop", key="agent_transcript_path")
    r = {x["message_id"]: x for x in rows(repo)}
    assert r["s1"]["subagent"] == "yes" and r["s1"]["output_tokens"] == "9" and r["m1"]["subagent"] == "no"


def test_two_sessions_in_one_repo_do_not_mix_or_duplicate(repo, tmp_path):
    apply.init(repo)
    a = write(tmp_path / "a.jsonl", [A("a1", sid="A"), A("a2", sid="A")])
    b = write(tmp_path / "b.jsonl", [A("b1", sid="B")])
    hook(repo, a, sid="A")
    hook(repo, b, sid="B")
    hook(repo, a, sid="A")
    assert sorted((x["session_id"], x["message_id"]) for x in rows(repo)) == [("A", "a1"), ("A", "a2"), ("B", "b1")]


def test_parallel_hooks_write_every_row_once(repo, tmp_path):
    apply.init(repo)
    files = [write(tmp_path / f"s{i}.jsonl", [A(f"s{i}m{j}", sid=f"S{i}") for j in range(5)]) for i in range(6)]
    procs = [subprocess.Popen([sys.executable, str(repo / ".claude/hooks/log_token_usage.py")], stdin=subprocess.PIPE, text=True,
                              env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}) for _ in files]
    for i, p in enumerate(procs):
        p.stdin.write(json.dumps({"session_id": f"S{i}", "hook_event_name": "Stop", "cwd": str(repo), "transcript_path": str(files[i])}))
        p.stdin.close()
    assert [p.wait(timeout=30) for p in procs] == [0] * 6
    ids = [x["message_id"] for x in rows(repo)]
    assert len(ids) == 30 and len(set(ids)) == 30


def test_stop_hook_opt_out_and_missing_transcript_are_silent(repo, tmp_path):
    apply.init(repo)
    tr = write(tmp_path / "t.jsonl", [A("m1")])
    hook(repo, tr, extra_env={"CCKIT_USAGE_LOG": "off"})
    hook(repo, tmp_path / "missing.jsonl")
    hook(repo, "")
    assert rows(repo) == []


def test_stop_hook_survives_garbage_input(repo):
    apply.init(repo)
    p = subprocess.run([sys.executable, str(repo / ".claude/hooks/log_token_usage.py")], input="not json at all", capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""


# ------------------------------------------------------------------ usage_report.py
def project_dir_for(home, repo):
    import re
    d = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
    d.mkdir(parents=True, exist_ok=True)
    return d


def report_run(repo, *args, env=None):
    p = subprocess.run([sys.executable, str(repo / ".claude/scripts/usage_report.py"), *args], capture_output=True, text=True, cwd=str(repo),
                       env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), **(env or {})}, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout


def seed_sessions(home, repo):
    d = project_dir_for(home, repo)
    write(d / "sess-old.jsonl", [A("o1", ts="2026-01-02T09:00:00Z", sid="sess-old", out=1000), A("o2", ts="2026-01-02T09:30:00Z", sid="sess-old", out=2000)])
    write(d / "sess-new.jsonl", [
        A("n1", ts="2026-07-01T10:00:00Z", sid="sess-new", inp=10, out=100, cr=1000, cw=200, tools=("Bash", "Read")),
        A("n1", ts="2026-07-01T10:00:01Z", sid="sess-new", inp=10, out=100, cr=1000, cw=200, tools=("Bash", "Read")),
        A("n2", ts="2026-07-01T10:20:00Z", sid="sess-new", inp=20, out=300, cr=50000, cw=100, effort="medium", tools=("Bash",)),
        json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": "2026-07-01T10:10:00Z"})])
    write(d / "sess-new" / "subagents" / "agent-x.jsonl", [A("sub1", model="claude-haiku-5-5", ts="2026-07-01T10:05:00Z", sid="sess-new", out=40, cr=0, cw=0, inp=7)])
    os.utime(d / "sess-old.jsonl", (time.time() - 90 * 86400,) * 2)               # old session: outside a 7-day window
    return d


def test_report_for_the_current_session_by_id_from_the_environment(repo, home):
    apply.init(repo)
    seed_sessions(home, repo)
    out = report_run(repo, env={"CLAUDE_CODE_SESSION_ID": "sess-new"})
    assert "Session sess-new" in out[:20] or "Session sess-ne" in out
    assert "requests:    2 main + 1 subagent | compactions 1 | tool calls 3 (Bash 2, Read 1)" in out
    assert "effort medium" in out and "cli 2.1.300" in out                          # newest main response's settings
    assert "context now: 50.1k of 200.0k tokens (25%)" in out                      # 20 + 50000 + 100
    assert "est. cost:   n/a" in out                                                # no prices configured: no invented number
    assert "SECRET" not in out


def test_report_latest_falls_back_to_the_newest_transcript(repo, home):
    apply.init(repo)
    d = seed_sessions(home, repo)
    os.utime(d / "sess-new.jsonl", (time.time(),) * 2)
    assert "Session sess-ne" in report_run(repo)
    os.utime(d / "sess-old.jsonl", (time.time() + 100,) * 2)
    assert "Session sess-ol" in report_run(repo)


def test_report_all_sessions_with_a_day_window_and_table(repo, home):
    apply.init(repo)
    seed_sessions(home, repo)
    out = report_run(repo, "--session", "all", "--days", "7")
    assert "sess-new"[:8] in out and "sess-old"[:8] not in out and "TOTAL" in out
    assert "claude-sonnet-5-5" in out and "claude-haiku-5-5" in out                 # subagent model appears
    everything = report_run(repo, "--session", "all")
    assert "2026-01-02" in everything and "2026-07-01" in everything


def test_report_json_is_machine_readable_and_counts_each_message_once(repo, home):
    apply.init(repo)
    seed_sessions(home, repo)
    d = json.loads(report_run(repo, "--session", "sess-new", "--json"))[0]
    assert d["main"]["requests"] == 2 and d["subagent"]["requests"] == 1 and d["compactions"] == 1       # n1 appears twice in the file
    assert (d["main"]["input"], d["main"]["output"], d["main"]["cache_read"], d["main"]["cache_write"]) == (30, 400, 51000, 300)
    assert d["subagent"]["output"] == 40 and d["context_tokens"] == 50120 and d["context_pct"] == 25.1
    assert d["estimated_cost_usd"] is None and d["tools"] == {"Bash": 2, "Read": 1}


def test_report_prices_come_from_cckit_toml(repo, home):
    (repo / "cckit.toml").write_text('[prices.sonnet]\ninput = 3.0\noutput = 15.0\ncache_read = 0.3\ncache_write = 3.75\n')
    apply.init(repo)
    seed_sessions(home, repo)
    d = json.loads(report_run(repo, "--session", "sess-new", "--json"))[0]
    expect = (30 * 3.0 + 400 * 15.0 + 51000 * 0.3 + 300 * 3.75) / 1e6
    assert d["estimated_cost_usd"] == round(expect, 4) and d["models_without_price"] == ["claude-haiku-5-5"]
    out = report_run(repo, "--session", "sess-new")
    assert f"${expect:.2f}" in out and "no price for: claude-haiku-5-5" in out


def test_report_warns_when_context_is_getting_full(repo, home):
    apply.init(repo)
    d = project_dir_for(home, repo)
    write(d / "big.jsonl", [A("b1", sid="big", inp=10, cr=130000, cw=0)])
    assert "getting full: /compact now" in report_run(repo, "--session", "big")


def test_report_with_no_transcripts_says_so(repo, home):
    apply.init(repo)
    p = subprocess.run([sys.executable, str(repo / ".claude/scripts/usage_report.py")], capture_output=True, text=True, cwd=str(repo),
                       env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)})
    assert "No Claude Code transcripts found" in p.stdout


def test_report_agrees_with_cckit_report_on_the_same_transcripts(repo, home):
    apply.init(repo)
    seed_sessions(home, repo)
    summary = report.summarise(report.collect(home / "projects"))
    mine = json.loads(report_run(repo, "--session", "all", "--json"))
    assert sum(s["main"]["output"] + s["subagent"]["output"] for s in mine) == sum(t.output for t in summary["by_day_model"].values())
    assert sum(s["main"]["requests"] + s["subagent"]["requests"] for s in mine) == sum(t.requests for t in summary["by_day_model"].values())


def test_report_only_prints_numbers_and_tool_names(repo, home):
    apply.init(repo)
    seed_sessions(home, repo)
    for args in ([], ["--session", "all"], ["--json", "--session", "all"]):
        assert "SECRET" not in report_run(repo, *args)


def test_usage_command_runs_the_report_scripts_with_a_narrow_permission(repo):
    apply.init(repo)
    cmd = read(repo / ".claude/commands/usage.md")
    assert "allowed-tools: Bash(python3 .claude/scripts/usage_report.py:*)" in cmd and "!`python3 .claude/scripts/usage_report.py`" in cmd
    assert "--session all --days 7" in cmd


def test_generated_scripts_run_on_old_pythons_grammar(repo):
    import ast
    apply.init(repo)
    for rel in (".claude/hooks/log_token_usage.py", ".claude/scripts/usage_report.py"):
        ast.parse(read(repo / rel), feature_version=(3, 8))


# ------------------------------------------------------------------ the transcript is flushed a moment after Stop fires
def load_log_module(repo):
    apply.init(repo)
    spec = importlib.util.spec_from_file_location("log_token_usage_under_test", repo / ".claude/hooks/log_token_usage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_settle_waits_until_the_transcript_stops_growing(repo, tmp_path, monkeypatch):
    mod = load_log_module(repo)
    sizes = iter([100, 150, 150, 999])
    sleeps = []
    monkeypatch.setattr(mod.os.path, "getsize", lambda p: next(sizes))
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
    mod.settle("x")
    assert len(sleeps) == 2                                                          # 100 -> 150 (grew), 150 -> 150 (stable): done


def test_settle_gives_up_after_a_bounded_time_and_tolerates_a_missing_file(repo, monkeypatch):
    mod = load_log_module(repo)
    n = [0]
    def grow(_):
        n[0] += 1
        return n[0]
    monkeypatch.setattr(mod.os.path, "getsize", grow)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    mod.settle("x")
    assert n[0] == 8                                                                 # never loops forever
    monkeypatch.setattr(mod.os.path, "getsize", lambda p: (_ for _ in ()).throw(OSError()))
    mod.settle("x")


def test_a_late_flush_is_caught_by_a_writer_that_finishes_while_the_hook_waits(repo, tmp_path):
    import threading
    apply.init(repo)
    tr = write(tmp_path / "t.jsonl", [A("m1")])

    def late_writer():
        time.sleep(0.12)
        with open(tr, "a", encoding="utf-8") as f:
            f.write(A("m2", ts="2026-07-01T10:05:00Z") + "\n")

    t = threading.Thread(target=late_writer)
    t.start()
    hook(repo, tr)
    t.join()
    hook(repo, tr)
    assert [x["message_id"] for x in rows(repo)] == ["m1", "m2"]                    # at worst the next event picks it up; never lost, never twice


def test_main_stop_catches_up_on_a_subagent_that_was_still_being_written(repo, tmp_path):
    apply.init(repo)
    main = write(tmp_path / "sess.jsonl", [A("m1")])
    subfile = tmp_path / "sess" / "subagents" / "agent-1.jsonl"
    write(subfile, [A("s1", model="claude-haiku-5-5", out=5)])
    hook(repo, subfile, event="SubagentStop", key="agent_transcript_path")           # the subagent's last message is not flushed yet
    with open(subfile, "a", encoding="utf-8") as f:
        f.write(A("s2", model="claude-haiku-5-5", out=9, ts="2026-07-01T10:06:00Z") + "\n")
    hook(repo, main)                                                                # no SubagentStop will ever come again: the next Stop picks it up
    r = {x["message_id"]: x for x in rows(repo)}
    assert set(r) == {"m1", "s1", "s2"} and r["s2"]["subagent"] == "yes" and r["s2"]["output_tokens"] == "9"
    hook(repo, main)
    assert len(rows(repo)) == 3


def test_subagent_files_of_other_sessions_are_not_swept_in(repo, tmp_path):
    apply.init(repo)
    mine = write(tmp_path / "mine.jsonl", [A("m1")])
    write(tmp_path / "other" / "subagents" / "agent-9.jsonl", [A("x1", model="claude-haiku-5-5")])
    hook(repo, mine)
    assert [x["message_id"] for x in rows(repo)] == ["m1"]


# ------------------------------------------------------------------ parallel sessions: one header, no lost offsets
def test_many_parallel_first_runs_write_the_header_exactly_once(repo, tmp_path):
    apply.init(repo)
    files = [write(tmp_path / f"p{i}.jsonl", [A(f"p{i}m{j}", sid=f"P{i}") for j in range(4)]) for i in range(14)]
    procs = [subprocess.Popen([sys.executable, str(repo / ".claude/hooks/log_token_usage.py")], stdin=subprocess.PIPE, text=True,
                              env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}) for _ in files]
    for i, p in enumerate(procs):
        p.stdin.write(json.dumps({"session_id": f"P{i}", "hook_event_name": "Stop", "cwd": str(repo), "transcript_path": str(files[i])}))
        p.stdin.close()
    assert [p.wait(timeout=60) for p in procs] == [0] * 14
    text = read(repo / ".claude/token_usage.csv")
    assert text.count("timestamp,session_id,message_id") == 1 and text.startswith("timestamp,")
    ids = [x["message_id"] for x in rows(repo)]
    assert len(ids) == 56 and len(set(ids)) == 56
    assert not (repo / ".claude/token_usage.csv.lock").exists()                    # the lock is released


def test_a_stale_lock_from_a_crashed_run_does_not_block_logging(repo, tmp_path):
    apply.init(repo)
    lock = repo / ".claude/token_usage.csv.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text("")
    os.utime(lock, (time.time() - 60,) * 2)                                          # a minute old: its owner is long gone
    tr = write(tmp_path / "t.jsonl", [A("m1")])
    hook(repo, tr)
    assert [x["message_id"] for x in rows(repo)] == ["m1"]


def test_a_held_lock_makes_the_run_skip_and_the_next_event_catches_up(repo, tmp_path):
    apply.init(repo)
    lock = repo / ".claude/token_usage.csv.lock"
    lock.write_text("")                                                              # fresh lock: another run is busy
    tr = write(tmp_path / "t.jsonl", [A("m1")])
    t0 = time.time()
    hook(repo, tr)
    assert time.time() - t0 < 6 and rows(repo) == []                                 # gave up after about 3 seconds, wrote nothing
    lock.unlink()
    hook(repo, tr)
    assert [x["message_id"] for x in rows(repo)] == ["m1"]                           # nothing was lost: the offset never moved


def test_gitignore_block_lists_csv_state_and_lock(repo):
    apply.init(repo)
    gi = read(repo / ".gitignore")
    assert "/.claude/token_usage.csv\n/.claude/token_usage.csv.state.json\n/.claude/token_usage.csv.lock\n" in gi


def test_claude_cannot_read_the_log_it_is_told_not_to_read(repo):
    apply.init(repo)
    deny = json.loads(read(repo / ".claude/settings.json"))["permissions"]["deny"]
    assert "Read(.claude/token_usage.csv)" in deny and "Read(.claude/token_usage.csv.state.json)" in deny
    md = read(repo / "CLAUDE.md")
    assert "never read it whole, use the report script" in md and ".claude/token_usage.csv" in md
