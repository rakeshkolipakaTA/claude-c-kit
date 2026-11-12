import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cckit import apply, cli, config, doctor, identity, render, report

ROOT = Path(__file__).resolve().parents[1]


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    return tmp_path


def settings(repo) -> dict:
    return json.loads(read(repo / ".claude" / "settings.json"))


# ------------------------------------------------------------------ config
def test_defaults_are_valid():
    assert config.validate(config.defaults()) == []


@pytest.mark.parametrize("path,value,fragment", [
    (("model", "effort"), "max", "effort"),
    (("context", "auto_compact_window"), 50_000, "auto_compact_window"),
    (("context", "auto_compact_window"), 2_000_000, "auto_compact_window"),
    (("context", "auto_compact_pct"), 0, "auto_compact_pct"),
    (("context", "auto_compact_pct"), 101, "auto_compact_pct"),
    (("subagents", "max_concurrent"), 0, "max_concurrent"),
    (("tools", "tool_search"), "sometimes", "tool_search"),
    (("tools", "tool_search"), "auto:150", "tool_search"),
    (("tools", "bash_max_output_chars"), 999_999, "bash_max_output_chars"),
    (("hooks", "tail_lines"), 1, "tail_lines"),
])
def test_validation_rejects_bad_values(path, value, fragment):
    cfg = config.defaults()
    cfg[path[0]][path[1]] = value
    errs = config.validate(cfg)
    assert any(fragment in e for e in errs), errs


def test_valid_tool_search_forms():
    for v in ("true", "false", "auto", "auto:5", "auto:100"):
        cfg = config.defaults()
        cfg["tools"]["tool_search"] = v
        assert config.validate(cfg) == [], v


def test_user_toml_overrides_and_keeps_other_defaults(repo):
    (repo / ".cckit/cckit.toml").write_text('[model]\ndefault = "claude-sonnet-5-5"\n[agents.explorer]\nmax_turns = 5\n')
    cfg = config.load(repo)
    assert cfg["model"]["default"] == "claude-sonnet-5-5"
    assert cfg["model"]["subagent"] == "haiku"
    assert cfg["agents"]["explorer"]["max_turns"] == 5 and cfg["agents"]["explorer"]["model"] == "haiku"


def test_invalid_toml_is_a_clear_error(repo):
    (repo / ".cckit/cckit.toml").write_text("[model\n")
    with pytest.raises(ValueError, match="not valid TOML"):
        config.load(repo)


# ------------------------------------------------------------------ the generated settings
def test_settings_contain_every_requested_control():
    s = render.build_settings(config.defaults())
    env = s["env"]
    assert s["model"] == "sonnet" and s["effortLevel"] == "medium"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"
    assert env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "4"
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "50"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
    assert env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] == "1"
    assert env["ENABLE_TOOL_SEARCH"] == "true"
    assert "Agent(model:opus)" in s["permissions"]["deny"]
    assert "Read(**/node_modules/**)" in s["permissions"]["deny"] and "Read(dist/**)" in s["permissions"]["deny"]      # nested packages too; dist/build stay root-anchored (a source folder may be called build)
    assert s["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert s["statusLine"]["type"] == "command"
    assert all(isinstance(v, str) for v in env.values()), "settings env values must be strings"


def test_max_thinking_tokens_only_when_it_can_work():
    cfg = config.defaults()
    cfg["thinking"]["max_tokens"] = 8000
    assert "MAX_THINKING_TOKENS" not in render.build_env(cfg)          # adaptive models ignore it
    cfg["thinking"]["fixed_budget"] = True
    env = render.build_env(cfg)
    assert env["MAX_THINKING_TOKENS"] == "8000" and env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] == "1"


def test_optional_switches():
    cfg = config.defaults()
    cfg["context"]["cap_context_to_200k"] = False
    cfg["model"]["subagent_force"] = True
    cfg["model"]["block_opus_subagents"] = False
    cfg["hooks"]["trim_test_output"] = False
    cfg["monitoring"]["statusline"] = False
    cfg["usage"]["log"] = False
    cfg["usage"]["scripts"] = False
    cfg["telemetry"]["enabled"] = False
    cfg["model"]["opus_reminder"] = False
    s = render.build_settings(cfg)
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT" not in s["env"]
    assert s["env"]["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    assert "Agent(model:opus)" not in s["permissions"]["deny"]
    assert "hooks" not in s and "statusLine" not in s


# ------------------------------------------------------------------ init
def test_init_creates_everything_and_is_idempotent(repo):
    r1 = apply.init(repo)
    assert r1.changed > 0
    for rel in (".cckit/cckit.toml", "CLAUDE.md", ".claude/settings.json", ".claude/agents/explorer.md", ".claude/agents/test-runner.md",
                ".claude/agents/reviewer.md", ".claude/rules/testing.md", ".cckit/hooks/trim_output.py", ".cckit/hooks/statusline.py",
                ".cckit/mcp-profiles/none.json", ".cckit/managed-settings.example.json", ".cckit/manifest.json",
                ".cckit/hooks/log_token_usage.py", ".cckit/scripts/usage_report.py", ".claude/commands/usage.md", ".gitignore"):
        assert (repo / rel).exists(), rel
    before = {p: read(p) for p in repo.rglob("*") if p.is_file()}
    r2 = apply.init(repo)
    assert r2.changed == 0, r2.lines
    after = {p: read(p) for p in repo.rglob("*") if p.is_file()}
    assert before == after


def test_init_dry_run_writes_nothing(repo):
    res = apply.init(repo, dry_run=True)
    assert res.lines and not list(repo.iterdir())


def test_init_keeps_users_own_settings_and_reports_conflicts(repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({
        "model": "opus", "permissions": {"allow": ["Bash(npm run lint)"], "deny": ["Read(./.env)"]},
        "env": {"FOO": "bar", "BASH_MAX_OUTPUT_LENGTH": "99999"}, "enabledPlugins": {"x": True}}))
    res = apply.init(repo)
    s = settings(repo)
    assert s["model"] == "opus" and s["env"]["BASH_MAX_OUTPUT_LENGTH"] == "99999" and s["env"]["FOO"] == "bar"
    assert s["permissions"]["allow"][0] == "Bash(npm run lint)" and "Read(./.env)" in s["permissions"]["deny"]
    assert s["permissions"]["allow"][1:] == ["Bash(python3 .cckit/scripts/usage_report.py:*)"]            # cckit's own rule is added after the user's
    assert s["enabledPlugins"] == {"x": True}
    assert any("kept your 'model'" in n for n in res.lines)
    apply.init(repo, force=True)
    s = settings(repo)
    assert s["model"] == "sonnet" and s["env"]["BASH_MAX_OUTPUT_LENGTH"] == "20000" and s["env"]["FOO"] == "bar"


def test_changing_config_removes_stale_entries_it_added(repo):
    apply.init(repo)
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT" in settings(repo)["env"]
    cfg = read(repo / ".cckit/cckit.toml").replace("cap_context_to_200k = true", "cap_context_to_200k = false")
    (repo / ".cckit/cckit.toml").write_text(cfg)
    apply.init(repo)
    s = settings(repo)
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT" not in s["env"]
    assert len([e for e in s["hooks"]["PreToolUse"] if render.is_cckit_hook(e)]) == 1      # not duplicated


def test_claude_md_block_is_replaced_and_user_text_survives(repo):
    (repo / "CLAUDE.md").write_text("# Mine\n\nKeep this.\n")
    apply.init(repo)
    text = read(repo / "CLAUDE.md")
    assert text.startswith("# Mine\n\nKeep this.\n") and render.MARK_END in text and "# Compact instructions" in text
    (repo / "CLAUDE.md").write_text(text + "\n## Added later\nMore.\n")
    apply.init(repo)
    text2 = read(repo / "CLAUDE.md")
    assert text2.count("cckit:begin") == 1 and "## Added later" in text2 and "Keep this." in text2


def test_edited_generated_files_are_not_overwritten_without_force(repo):
    apply.init(repo)
    agent = repo / ".claude" / "agents" / "explorer.md"
    agent.write_text(read(agent) + "\nMy extra rule.\n")
    res = apply.init(repo)
    assert "My extra rule." in read(agent) and any(l.startswith("kept") and "explorer.md" in l for l in res.lines)
    apply.init(repo, force=True)
    assert "My extra rule." not in read(agent)


def test_untouched_files_follow_config_changes(repo):
    apply.init(repo)
    (repo / ".cckit/cckit.toml").write_text(read(repo / ".cckit/cckit.toml").replace("max_turns = 15", "max_turns = 6"))
    apply.init(repo)
    assert "maxTurns: 6" in read(repo / ".claude" / "agents" / "explorer.md")


def test_disabling_an_agent_removes_its_untouched_file(repo):
    apply.init(repo)
    assert (repo / ".claude/agents/reviewer.md").exists()
    (repo / ".cckit/cckit.toml").write_text(read(repo / ".cckit/cckit.toml").replace("[agents.reviewer]", "[agents.reviewer]\nenabled = false"))
    res = apply.init(repo)
    assert not (repo / ".claude/agents/reviewer.md").exists() and any(l.startswith("removed") for l in res.lines)
    assert not doctor.run(repo, check_claude=False).failed


def test_disabled_agent_that_you_edited_is_kept(repo):
    apply.init(repo)
    f = repo / ".claude/agents/reviewer.md"
    f.write_text(read(f) + "\nmine\n")
    (repo / ".cckit/cckit.toml").write_text(read(repo / ".cckit/cckit.toml").replace("[agents.reviewer]", "[agents.reviewer]\nenabled = false"))
    apply.init(repo)
    assert f.exists() and "mine" in read(f)


def test_broken_settings_json_is_never_overwritten(repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{ not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        apply.init(repo)
    assert read(repo / ".claude" / "settings.json") == "{ not json"


def test_invalid_cckit_toml_aborts_before_writing(repo):
    (repo / ".cckit/cckit.toml").write_text("[model]\neffort = 'max'\n")
    with pytest.raises(ValueError, match="effort"):
        apply.init(repo)
    assert not (repo / ".claude").exists()


# ------------------------------------------------------------------ agents, rules, claude.md
def frontmatter(path: Path) -> dict:
    return doctor._frontmatter(read(path))


def test_agent_files_have_valid_frontmatter(repo):
    apply.init(repo)
    for name in ("explorer", "test-runner", "reviewer"):
        fm = frontmatter(repo / ".claude" / "agents" / f"{name}.md")
        assert fm["name"] == name and fm["model"] in doctor.AGENT_MODELS and int(fm["maxTurns"]) > 0
    assert frontmatter(repo / ".claude/agents/explorer.md")["tools"] == "Read, Grep, Glob"      # read-only
    assert "Edit" not in frontmatter(repo / ".claude/agents/reviewer.md")["tools"]


def test_generated_claude_md_is_short(repo):
    apply.init(repo)
    assert len(read(repo / "CLAUDE.md").splitlines()) < 40


def test_rule_has_path_scope(repo):
    apply.init(repo)
    assert read(repo / ".claude/rules/testing.md").startswith('---\npaths:\n  - "tests/**"')


# ------------------------------------------------------------------ hook script (the real generated file)
def run_script(path: Path, payload) -> str:
    inp = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(path)], input=inp, capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("cmd", ["pytest", "pytest -q tests/test_a.py", "python -m pytest -x", "npm test", "go test ./...", "cargo test --all", "mvn test"])
def test_hook_rewrites_plain_test_runs(repo, cmd):
    apply.init(repo)
    out = json.loads(run_script(repo / ".cckit/hooks/trim_output.py", {"tool_name": "Bash", "tool_input": {"command": cmd, "description": "d"}}))
    upd = out["hookSpecificOutput"]["updatedInput"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    script = str(repo / ".cckit/hooks/trim_output.py").replace("\\", "/")
    assert upd["command"] == f'set -o pipefail; {cmd} 2>&1 | "{sys.executable}" "{script}" --trim' and upd["description"] == "d"


@pytest.mark.parametrize("cmd", ["pytest | grep x", "ls; pytest", "pytest && rm -rf x", "pytest > out.txt", "echo $(pytest)", "pytest # c", "cat file", "git status", "pytest `whoami`"])
def test_hook_leaves_everything_else_alone(repo, cmd):
    apply.init(repo)
    assert run_script(repo / ".cckit/hooks/trim_output.py", {"tool_name": "Bash", "tool_input": {"command": cmd}}).strip() == "{}"


def test_hook_ignores_other_tools_and_garbage(repo):
    apply.init(repo)
    h = repo / ".cckit/hooks/trim_output.py"
    assert run_script(h, {"tool_name": "Read", "tool_input": {"file_path": "x"}}).strip() == "{}"
    assert run_script(h, "not json").strip() == "{}"


def test_hook_keeps_the_exit_code(repo):
    apply.init(repo)
    out = json.loads(run_script(repo / ".cckit/hooks/trim_output.py", {"tool_name": "Bash", "tool_input": {"command": "pytest"}}))
    cmd = out["hookSpecificOutput"]["updatedInput"]["command"].replace("pytest 2>&1", "false 2>&1", 1)
    assert subprocess.run(["bash", "-c", cmd]).returncode != 0                      # the failing command's status survives the filter


def test_hook_tail_lines_follow_config(repo):
    (repo / ".cckit/cckit.toml").write_text("[hooks]\ntail_lines = 40\n")
    apply.init(repo)
    out = json.loads(run_script(repo / ".cckit/hooks/trim_output.py", {"tool_name": "Bash", "tool_input": {"command": "pytest"}}))
    assert "TAIL_LINES = 40" in read(repo / ".cckit/hooks/trim_output.py")


def test_statusline(repo):
    apply.init(repo)
    s = repo / ".cckit/hooks/statusline.py"
    assert run_script(s, {"model": {"display_name": "Sonnet 5.5"}, "context_window": {"used_percentage": 20}, "cost": {"total_cost_usd": 0.5}}).strip() == "[Sonnet 5.5] ctx 20% $0.50"
    assert "/compact" in run_script(s, {"context_window": {"used_percentage": 75}})
    assert run_script(s, {}).strip() == "[Claude]"
    assert run_script(s, "garbage").strip() == "[Claude]"


# ------------------------------------------------------------------ doctor
def test_doctor_clean_after_init(repo):
    apply.init(repo)
    rep = doctor.run(repo, check_claude=False)
    assert not rep.failed, rep.render()


def test_doctor_fails_without_init(repo):
    rep = doctor.run(repo, check_claude=False)
    assert rep.failed


def test_doctor_flags_long_claude_md(repo):
    apply.init(repo)
    (repo / "CLAUDE.md").write_text(read(repo / "CLAUDE.md") + "\n".join(f"line {i}" for i in range(300)))
    rep = doctor.run(repo, check_claude=False)
    assert rep.failed and "CLAUDE.md has" in rep.render()


def test_doctor_flags_long_rules_file(repo):
    apply.init(repo)
    (repo / ".claude/rules/big.md").write_text("\n".join("x" for _ in range(250)))
    assert "big.md" in doctor.run(repo, check_claude=False).render()


def test_doctor_detects_removed_settings(repo):
    apply.init(repo)
    s = settings(repo)
    del s["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"]
    (repo / ".claude/settings.json").write_text(json.dumps(s))
    rep = doctor.run(repo, check_claude=False)
    assert rep.failed and "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS" in rep.render()


def test_doctor_warns_when_thinking_budget_would_be_ignored(repo):
    (repo / ".cckit/cckit.toml").write_text("[thinking]\nmax_tokens = 8000\n")
    apply.init(repo)
    out = doctor.run(repo, check_claude=False).render()
    assert "max_tokens is ignored" in out


def test_doctor_warns_about_telemetry_in_repo_settings(repo):
    apply.init(repo)
    s = settings(repo)
    s["env"]["OTEL_METRICS_EXPORTER"] = "otlp"
    (repo / ".claude/settings.json").write_text(json.dumps(s))
    assert "ignored by Claude Code" in doctor.run(repo, check_claude=False).render()


def test_doctor_flags_agent_without_frontmatter(repo):
    apply.init(repo)
    (repo / ".claude/agents/explorer.md").write_text("no frontmatter")
    assert doctor.run(repo, check_claude=False).failed


# ------------------------------------------------------------------ report (synthetic transcript)
def transcript(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def asst(mid, model, inp, out, cr, cw, ts="2026-11-10T10:00:00Z", side=False, sid="s1"):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts, "isSidechain": side,
            "message": {"id": mid, "model": model, "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}}


def test_report_dedupes_streamed_blocks_and_skips_synthetic(tmp_path):
    base = tmp_path / "projects"
    transcript(base / "proj-a" / "s1.jsonl", [
        asst("m1", "claude-sonnet-5-5", 10, 5, 0, 1000),          # first block of a response ...
        asst("m1", "claude-sonnet-5-5", 10, 40, 0, 1000),         # ... the same response with its final count
        asst("m2", "claude-sonnet-5-5", 20, 60, 1000, 0),
        asst("m3", "<synthetic>", 0, 0, 0, 0),
        asst("m4", "claude-haiku-5-5", 5, 5, 0, 0, side=True),
        {"type": "user", "message": {"content": "secret text that must never be printed"}},
    ])
    summary = report.summarise(report.collect(base))
    t = summary["by_day_model"][("2026-11-10", "claude-sonnet-5-5")]
    assert (t.requests, t.input, t.output, t.cache_read, t.cache_write) == (2, 30, 100, 1000, 1000)
    assert ("2026-11-10", "<synthetic>") not in summary["by_day_model"]
    s = summary["sessions"]["s1"]
    assert s.side.requests == 1 and s.tot.requests == 2 and s.max_context == 1020
    text = report.render(summary)
    assert "claude-sonnet-5-5" in text and "secret text" not in text


def test_report_cost_only_with_prices(tmp_path):
    base = tmp_path / "p"
    transcript(base / "proj" / "s.jsonl", [asst("m1", "claude-sonnet-5-5", 1_000_000, 1_000_000, 0, 0)])
    summary = report.summarise(report.collect(base))
    assert "est. $" not in report.render(summary)
    text = report.render(summary, prices={"sonnet": {"input": 3.0, "output": 15.0}})
    assert "18.00" in text


def test_report_flags_long_context_sessions(tmp_path):
    base = tmp_path / "p"
    transcript(base / "proj" / "s.jsonl", [asst("m1", "claude-sonnet-5-5", 1000, 10, 400_000, 0)])
    assert "long context" in report.render(report.summarise(report.collect(base)))


def test_report_day_filter_and_empty(tmp_path):
    base = tmp_path / "p"
    transcript(base / "proj" / "s.jsonl", [asst("m1", "claude-sonnet-5-5", 1, 1, 0, 0, ts="2020-01-01T00:00:00Z")])
    assert report.render(report.summarise(report.collect(base, days=7))) == "No Claude Code usage found."


# ------------------------------------------------------------------ run / cli
def test_run_builds_strict_mcp_command(repo):
    apply.init(repo)
    cmd = cli.build_run_command(repo, "none", None, "low", ["-p", "hi"])
    assert cmd[:3] == ["claude", "--strict-mcp-config", "--mcp-config"] and cmd[3].endswith("none.json") and cmd[-4:] == ["--effort", "low", "-p", "hi"]


def test_run_unknown_profile_lists_available(repo):
    apply.init(repo)
    with pytest.raises(ValueError, match="Available: none"):
        cli.build_run_command(repo, "docs", None, None, [])


def test_mcp_profiles_come_from_toml(repo):
    (repo / ".cckit/cckit.toml").write_text('[mcp.profiles.docs.servers.my-docs]\ntype = "http"\nurl = "https://example.com/mcp"\n')
    apply.init(repo)
    data = json.loads(read(repo / ".cckit/mcp-profiles/docs.json"))
    assert data == {"mcpServers": {"my-docs": {"type": "http", "url": "https://example.com/mcp"}}}


def test_cli_end_to_end(repo, capsys):
    assert cli.main(["init", str(repo)]) == 0
    assert cli.main(["doctor", str(repo), "--no-claude"]) == 0
    assert cli.main(["print-settings", str(repo)]) == 0
    assert '"model": "sonnet"' in capsys.readouterr().out
    assert cli.main(["run", "--path", str(repo), "--profile", "none", "--dry-run", "--", "-p", "x"]) == 0
    assert "--strict-mcp-config" in capsys.readouterr().out


def test_cli_init_error_exit_code(repo, capsys):
    (repo / ".cckit/cckit.toml").write_text("[context]\nauto_compact_pct = 500\n")
    assert cli.main(["init", str(repo)]) == 2
    assert "auto_compact_pct" in capsys.readouterr().err


def test_managed_example_has_telemetry_and_limits():
    cfg = config.defaults()
    cfg["monitoring"]["otel_endpoint"] = "http://otel.corp:4317"
    m = json.loads(render.build_managed_example(cfg))
    assert m["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and m["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel.corp:4317"
    assert m["availableModels"] == ["sonnet", "haiku"] and m["maxEffortLevel"] == "high"


def test_python_dash_m_cckit_works(tmp_path):
    import os, subprocess, sys
    src = str(Path(__file__).resolve().parents[1] / "src")
    r = subprocess.run([sys.executable, "-m", "cckit", "--version"], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": src})
    assert r.returncode == 0 and r.stdout.startswith("cckit ")


# ------------------------------------------------------------------ the Opus reminder
def opus_run(repo, transcript=None, env=None, cwd=None):
    import subprocess, sys
    payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(cwd or repo), "prompt": "do the thing"}
    if transcript:
        payload["transcript_path"] = str(transcript)
    p = subprocess.run([sys.executable, str(repo / ".cckit/hooks/opus_reminder.py")], input=json.dumps(payload), capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), **(env or {})}, timeout=30)
    assert p.returncode == 0 and p.stderr == ""
    return p.stdout


def model_line(model, side=False):
    return json.dumps({"type": "assistant", "isSidechain": side, "message": {"id": "m" + model, "model": model, "usage": {"input_tokens": 1, "output_tokens": 1}}})


def test_reminder_hook_is_installed_by_default_and_listed_in_claude_md(repo):
    apply.init(repo)
    s = json.loads(read(repo / ".claude/settings.json"))
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["args"] == ["${CLAUDE_PROJECT_DIR}/.cckit/hooks/opus_reminder.py"]
    assert (repo / ".cckit/hooks/opus_reminder.py").exists() and os.access(repo / ".cckit/hooks/opus_reminder.py", os.X_OK)
    assert 'line starting "Model tip:"' in read(repo / "CLAUDE.md")


def test_reminder_only_on_opus(repo, tmp_path):
    apply.init(repo)
    t = tmp_path / "t.jsonl"
    t.write_text(model_line("claude-sonnet-5-5") + "\n" + model_line("claude-opus-5-5") + "\n")
    out = json.loads(opus_run(repo, t))["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit" and "Model tip:" in out["additionalContext"] and "/model sonnet" in out["additionalContext"]
    t.write_text(model_line("claude-opus-5-5") + "\n" + model_line("claude-sonnet-5-5") + "\n")           # switched to Sonnet: silent and free
    assert opus_run(repo, t) == ""
    t.write_text(model_line("claude-opus-5-5") + "\n" + model_line("claude-opus-5-5", side=True).replace("opus", "haiku") + "\n")   # a subagent's model does not count
    assert opus_run(repo, t) != ""


def test_reminder_before_the_first_response_uses_the_configured_model(repo, tmp_path, monkeypatch):
    apply.init(repo)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert opus_run(repo) == ""                                          # settings.json says sonnet
    assert "Model tip:" in opus_run(repo, env={"ANTHROPIC_MODEL": "opus"})
    s = json.loads(read(repo / ".claude/settings.json"))
    s["model"] = "claude-opus-5-5"
    (repo / ".claude/settings.json").write_text(json.dumps(s))
    assert "Model tip:" in opus_run(repo, tmp_path / "missing.jsonl")


def test_reminder_can_be_switched_off(repo, tmp_path):
    apply.init(repo)
    t = tmp_path / "t.jsonl"
    t.write_text(model_line("claude-opus-5-5") + "\n")
    assert opus_run(repo, t, env={"CCKIT_OPUS_REMINDER": "off"}) == ""
    (repo / ".cckit/cckit.toml").write_text("[model]\nopus_reminder = false\n")
    apply.init(repo)
    s = json.loads(read(repo / ".claude/settings.json"))
    assert "UserPromptSubmit" not in s.get("hooks", {}) and not (repo / ".cckit/hooks/opus_reminder.py").exists()
    assert "Model tip:" not in read(repo / "CLAUDE.md")


def test_reminder_survives_garbage_and_keeps_users_own_prompt_hooks(repo):
    import subprocess, sys
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
    apply.init(repo)
    entries = json.loads(read(repo / ".claude/settings.json"))["hooks"]["UserPromptSubmit"]
    assert len(entries) == 2 and any("echo mine" in json.dumps(e) for e in entries)
    p = subprocess.run([sys.executable, str(repo / ".cckit/hooks/opus_reminder.py")], input="not json", capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""


# ------------------------------------------------------------------ uninstall / install / reinstall
def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file() and ".git" not in p.parts}


USER_SETTINGS = {"permissions": {"allow": ["Bash(make test)"], "deny": ["Read(secrets/**)"]}, "model": "opus", "env": {"MINE": "1"},
                 "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}


def lived_in(repo):
    """A repo that already had its own CLAUDE.md, .gitignore and settings (CRLF line endings in the markdown on purpose)."""
    (repo / "CLAUDE.md").write_bytes(b"# Our project\r\nWe use tabs.\r\n")
    (repo / ".gitignore").write_text("dist/\n*.log\n")
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text(json.dumps(USER_SETTINGS, indent=2) + "\n")
    return tree(repo)


def test_uninstall_gives_back_exactly_what_the_repo_had(repo):
    before = lived_in(repo)
    apply.init(repo)
    assert tree(repo) != before
    assert cli.main(["uninstall", str(repo), "--yes"]) == 0
    after = tree(repo)
    for k, v in before.items():
        assert after[k] == v, k                                                       # CLAUDE.md (CRLF too), .gitignore and settings.json: byte for byte
    assert set(after) - set(before) == {".cckit/cckit.toml"}                         # only your own config is left behind
    assert not (repo / ".cckit/hooks").exists() and not (repo / ".claude/agents").exists() and not (repo / ".claude/commands").exists()


def test_uninstall_purge_removes_the_config_key_and_log_and_what_cckit_created(repo):
    apply.init(repo)                                                                  # a bare repo: cckit created CLAUDE.md, .gitignore and settings.json itself
    (repo / ".cckit/key.json").write_text('{"key": "abcd1234"}')
    (repo / ".cckit/token_usage.csv").write_text("timestamp\n")
    (repo / ".cckit/token_usage.csv.state.json").write_text("{}")
    assert cli.main(["uninstall", str(repo), "--purge", "--yes"]) == 0
    assert tree(repo) == {} and not (repo / ".cckit").exists() and not (repo / ".claude").exists()


def test_uninstall_without_purge_keeps_settings_key_and_log_and_init_restores_the_setup(repo):
    apply.init(repo)
    (repo / ".cckit/cckit.toml").write_text(read(repo / ".cckit/cckit.toml").replace("tail_lines = 100", "tail_lines = 33"))
    (repo / ".cckit/key.json").write_text('{"key": "abcd1234"}')
    (repo / ".cckit/token_usage.csv").write_text("timestamp\n1\n")
    first = tree(repo)
    cli.main(["uninstall", str(repo), "--yes"])
    left = tree(repo)
    assert set(left) == {".cckit/cckit.toml", ".cckit/key.json", ".cckit/token_usage.csv"}
    apply.init(repo)
    again = tree(repo)
    assert set(again) == set(first) and "tail_lines = 33" in again[".cckit/cckit.toml"].decode()
    assert again[".cckit/key.json"] == first[".cckit/key.json"] and again[".claude/settings.json"] == first[".claude/settings.json"]


def test_uninstall_keeps_a_file_you_edited_unless_forced(repo):
    apply.init(repo)
    mine = repo / ".claude/agents/explorer.md"
    mine.write_text(read(mine) + "\nMy own rule.\n")
    cli.main(["uninstall", str(repo), "--yes"])
    assert mine.exists() and "My own rule." in read(mine) and not (repo / ".claude/agents/reviewer.md").exists()
    cli.main(["uninstall", str(repo), "--yes", "--force"])
    assert not mine.exists()


def test_uninstall_dry_run_and_a_missing_confirmation_change_nothing(repo, monkeypatch, capsys):
    before = lived_in(repo)
    apply.init(repo)
    snap = tree(repo)
    assert cli.main(["uninstall", str(repo), "--dry-run"]) == 0 and tree(repo) == snap
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["uninstall", str(repo)]) == 2 and tree(repo) == snap              # no terminal and no --yes: refuses
    assert "--yes" in capsys.readouterr().err
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda p="": "")                             # Enter = no
    assert cli.main(["uninstall", str(repo)]) == 0 and tree(repo) == snap
    monkeypatch.setattr("builtins.input", lambda p="": "y")
    assert cli.main(["uninstall", str(repo)]) == 0 and tree(repo) != snap and before["CLAUDE.md"] == tree(repo)["CLAUDE.md"]


def test_uninstall_without_a_manifest_removes_only_unedited_generated_files(repo):
    apply.init(repo)
    (repo / ".cckit/manifest.json").unlink()
    mine = repo / ".claude/rules/testing.md"
    mine.write_text("my rules\n")
    cli.main(["uninstall", str(repo), "--yes"])
    assert mine.exists() and not (repo / ".cckit/hooks/log_token_usage.py").exists() and not (repo / ".claude/agents/explorer.md").exists()
    assert "hooks" not in json.loads(read(repo / ".claude/settings.json")) if (repo / ".claude/settings.json").exists() else True


def test_uninstall_refuses_a_lone_marker_and_changes_nothing(repo, capsys):
    apply.init(repo)
    (repo / "CLAUDE.md").write_text("# Mine\n<!-- cckit:begin -->\nno end marker\n")
    snap = tree(repo)
    assert cli.main(["uninstall", str(repo), "--yes"]) == 2 and tree(repo) == snap
    assert "marker" in capsys.readouterr().err


def test_uninstall_on_a_repo_without_cckit_is_harmless(repo, capsys):
    (repo / "CLAUDE.md").write_text("# Mine\n")
    assert cli.main(["uninstall", str(repo), "--yes"]) == 0 and tree(repo) == {"CLAUDE.md": b"# Mine\n"}
    assert "Nothing" in capsys.readouterr().out


def test_reinstall_regenerates_what_was_broken_and_keeps_your_settings(repo, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    apply.init(repo)
    (repo / ".cckit/cckit.toml").write_text(read(repo / ".cckit/cckit.toml").replace("tail_lines = 100", "tail_lines = 44"))
    apply.init(repo)
    hook = repo / ".cckit/hooks/trim_output.py"
    hook.write_text("broken\n")
    (repo / ".claude/agents/reviewer.md").unlink()
    assert cli.main(["reinstall", str(repo), "--yes"]) == 0
    assert "broken" not in read(hook) and "44" in read(hook) and (repo / ".claude/agents/reviewer.md").exists()   # a fresh copy, from the kept config
    assert "tail_lines = 44" in read(repo / ".cckit/cckit.toml")
    assert cli.main(["doctor", str(repo), "--no-claude"]) == 0


def test_install_is_init(repo):
    assert cli.main(["install", str(repo)]) == 0 and (repo / ".cckit/cckit.toml").exists() and (repo / ".claude/settings.json").exists()


def test_uninstall_leaves_this_machines_registration_alone(repo):
    identity.save_identity("https://e/api/usage", "ada.lovelace@tigeranalytics.com", "", "tok-ada")
    apply.init(repo)
    cli.main(["uninstall", str(repo), "--purge", "--yes"])
    assert identity.get_identity("https://e/api/usage")["token"] == "tok-ada"


def trim_run(repo, lines, tail=None):
    script = repo / ".cckit/hooks/trim_output.py"
    return subprocess.run([sys.executable, str(script), "--trim"], input="\n".join(lines) + "\n", capture_output=True, text=True, check=True).stdout


def test_trim_keeps_failures_from_the_middle_and_the_summary(repo):
    apply.init(repo)                                                                  # tail_lines = 100, head 10, at most 60 failure lines
    lines = [f"collected {i}" for i in range(500)]
    lines[200] = "FAILED tests/a.py::test_one - AssertionError: 1 != 2"
    lines[300] = "E   Traceback (most recent call last):"
    lines[-1] = "== 2 failed, 498 passed in 3.2s =="
    out = trim_run(repo, lines)
    assert "FAILED tests/a.py::test_one" in out and "Traceback" in out and "2 failed, 498 passed" in out and "collected 0" in out
    assert "lines left out" in out and out.count("\n") < 200                           # bounded: head + failures + tail + notes


def test_trim_passes_short_output_whole_and_caps_the_failure_lines(repo):
    apply.init(repo)
    short = [f"ok {i}" for i in range(50)]
    assert trim_run(repo, short) == "\n".join(short) + "\n"                            # nothing left out when it is short
    noisy = ["FAILED test_%d" % i for i in range(1000)]
    out = trim_run(repo, noisy)
    assert out.count("FAILED") <= 10 + 60 + 100 and out.count("\n") <= 10 + 60 + 100 + 10   # a run where everything fails stays bounded


# ------------------------------------------------------------------ coding standards and language rules
def rule_files(repo):
    return sorted(p.name for p in (repo / ".claude/rules").glob("*.md"))


def test_rules_are_generated_and_the_language_ones_only_for_languages_in_the_repo(repo):
    (repo / "app").mkdir()
    (repo / "app/main.py").write_text("print(1)\n")
    (repo / "web").mkdir()
    (repo / "web/package.json").write_text("{}")
    (repo / "node_modules/x").mkdir(parents=True)
    (repo / "node_modules/x/Thing.java").write_text("class Thing {}")                # dependency folders are not looked into
    apply.init(repo)
    assert rule_files(repo) == ["coding-standards.md", "git-workflow.md", "python.md", "testing.md", "typescript.md"]
    py = read(repo / ".claude/rules/python.md")
    assert py.startswith('---\npaths:\n  - "**/*.py"\n---')                         # loaded only when Claude touches Python files
    assert not read(repo / ".claude/rules/coding-standards.md").startswith("---")     # general standards: always loaded, and short
    assert sum(len(read(repo / f".claude/rules/{n}").splitlines()) for n in ("coding-standards.md", "git-workflow.md")) < 30


def test_rules_can_be_switched_off_or_chosen(repo):
    (repo / "A.java").write_text("class A {}")
    (repo / ".cckit/cckit.toml").write_text('[rules]\ncoding_standards = false\ngit_workflow = false\nlanguages = ["python"]\n')
    apply.init(repo)
    assert rule_files(repo) == ["python.md", "testing.md"]                           # the list wins over what was detected
    (repo / ".cckit/cckit.toml").write_text('[rules]\nlanguages = []\n')
    apply.init(repo)
    assert rule_files(repo) == ["coding-standards.md", "git-workflow.md", "testing.md"]        # python.md was ours and unedited: removed
    (repo / ".cckit/cckit.toml").write_text('[rules]\nlanguages = ["cobol"]\n')
    with pytest.raises(ValueError):
        apply.init(repo)


def test_an_edited_rule_file_is_never_overwritten_and_survives_uninstall(repo):
    apply.init(repo)
    mine = repo / ".claude/rules/coding-standards.md"
    mine.write_text("# Our standards\n- Tabs, always.\n")
    apply.init(repo)
    assert read(mine) == "# Our standards\n- Tabs, always.\n"
    cli.main(["uninstall", str(repo), "--yes"])
    assert mine.exists() and read(mine) == "# Our standards\n- Tabs, always.\n"


def test_effort_default_is_the_models_own_and_deny_globs_reach_nested_folders(repo):
    s = render.build_settings(config.defaults())
    assert s["effortLevel"] == "medium"
    deny = s["permissions"]["deny"]
    assert "Read(**/node_modules/**)" in deny and "Read(**/venv/**)" in deny and "Read(dist/**)" in deny and "Read(**/dist/**)" not in deny
    assert 'unless it may have changed' in render.build_claude_md_block(config.defaults())
