"""Turns a validated cckit config into file contents. Nothing here touches the disk (except reading assets)."""
from __future__ import annotations

import json
from importlib import resources

from . import config

MARK_BEGIN = "<!-- cckit:begin - managed by cckit. Edit .cckit/cckit.toml and run `cckit init`; text between the markers is rewritten -->"
MARK_END = "<!-- cckit:end -->"
HOOK_MARKERS = ("trim_output.py", "compact_report.py", "log_token_usage.py", "opus_reminder.py")      # identify the hook entries cckit owns inside settings.json
COMPACT_SCRIPT = ".cckit/hooks/compact_report.py"
LOG_SCRIPT = ".cckit/hooks/log_token_usage.py"
OPUS_SCRIPT = ".cckit/hooks/opus_reminder.py"
REPORT_SCRIPT = ".cckit/scripts/usage_report.py"
USAGE_COMMAND = ".claude/commands/usage.md"
GITIGNORE_BEGIN = "# cckit:begin - managed by cckit (local usage log); edit .cckit/cckit.toml and run `cckit init`"
GITIGNORE_END = "# cckit:end"
STATUS_SCRIPT = ".cckit/hooks/statusline.py"
TRIM_SCRIPT = ".cckit/hooks/trim_output.py"


def asset(name: str) -> str:
    return resources.files("cckit").joinpath("assets", name).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- settings.json
def build_env(cfg: dict) -> dict[str, str]:
    """Environment variables for the project's settings.json. Every name here is documented by Anthropic."""
    c, t, s, tl, m = cfg["context"], cfg["thinking"], cfg["subagents"], cfg["tools"], cfg["model"]
    env: dict[str, str] = {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(c["auto_compact_window"]),
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(c["auto_compact_pct"]),
        "CLAUDE_CODE_SUBAGENT_MODEL": m["subagent"],
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(s["max_concurrent"]),
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": str(s["max_depth"]),
        "ENABLE_TOOL_SEARCH": str(tl["tool_search"]),
        "BASH_MAX_OUTPUT_LENGTH": str(tl["bash_max_output_chars"]),
        "MAX_MCP_OUTPUT_TOKENS": str(tl["mcp_max_output_tokens"]),
    }
    if c["cap_context_to_200k"]:
        env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "1"
    if m["subagent_force"]:
        env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] = "1"
    if t["fixed_budget"]:
        env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"
        if t["max_tokens"]:
            env["MAX_THINKING_TOKENS"] = str(t["max_tokens"])
    return env


def build_deny(cfg: dict) -> list[str]:
    rules = [f"Read({p})" for p in cfg["permissions"]["deny_reads"]]
    if cfg["usage"]["log"]:
        p = cfg["usage"]["csv_path"].replace("\\", "/")
        rules += [f"Read({p})", f"Read({p}.state.json)"]               # the log is for the report script, not for the model
    rules.append(f"Read({config.KEY_NAME})")                           # the team key is for cckit's hooks, never for the model
    if cfg["model"]["block_opus_subagents"]:
        rules.append("Agent(model:opus)")
    return rules


def hook_entry(cfg: dict) -> dict:
    # exec form (command + args) avoids shell quoting problems with the project path
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": cfg["hooks"]["python"], "args": ["${CLAUDE_PROJECT_DIR}/" + TRIM_SCRIPT]}],
    }


def compact_hook_entry(cfg: dict) -> dict:
    """Reports each compaction. async: it never delays Claude Code; the script prints nothing (its output would feed the summary)."""
    entry: dict = {"hooks": [{"type": "command", "command": cfg["hooks"]["python"], "args": ["${CLAUDE_PROJECT_DIR}/" + COMPACT_SCRIPT],
                              "timeout": 25, "async": True}]}
    if sorted(cfg["telemetry"]["events"]) != ["auto", "manual"]:
        entry["matcher"] = cfg["telemetry"]["events"][0]
    return entry


def build_allow(cfg: dict) -> list[str]:
    """Claude may run the usage report without asking: it only reads local transcripts and prints numbers."""
    return [f"Bash(python3 {REPORT_SCRIPT}:*)".replace("python3", cfg["hooks"]["python"])] if cfg["usage"]["scripts"] else []


def stop_hook_on(cfg: dict) -> bool:
    """The Stop / SubagentStop hook is needed for the local CSV, for posting usage to the team app, or both."""
    return bool(cfg["usage"]["log"] or config.stop_report_active(cfg))


def opus_hook_entry(cfg: dict) -> dict:
    """UserPromptSubmit: while Opus is in use, adds the one-line model-tip instruction. Prints nothing on any other model."""
    return {"hooks": [{"type": "command", "command": cfg["hooks"]["python"], "args": ["${CLAUDE_PROJECT_DIR}/" + OPUS_SCRIPT], "timeout": 10}]}


def log_hook_entry(cfg: dict) -> dict:
    """Stop / SubagentStop: append token counts to the local CSV. Synchronous on purpose: a background hook is killed when a headless run
    (`claude -p`, the SDK) exits right after the last response, and this one only reads the new transcript lines (milliseconds). Prints nothing."""
    return {"hooks": [{"type": "command", "command": cfg["hooks"]["python"], "args": ["${CLAUDE_PROJECT_DIR}/" + LOG_SCRIPT], "timeout": 20}]}


def session_hook_entry(cfg: dict) -> dict:
    """Runs when a session starts: registers the developer once (company e-mail from git) or asks Claude to ask. Synchronous, because
    it may hand a sentence to Claude; it exits at once when this machine is already registered."""
    return {"matcher": "startup|resume",
            "hooks": [{"type": "command", "command": cfg["hooks"]["python"], "args": ["${CLAUDE_PROJECT_DIR}/" + COMPACT_SCRIPT], "timeout": 30}]}


def build_settings(cfg: dict) -> dict:
    """The part of .claude/settings.json that cckit owns."""
    out: dict = {"model": cfg["model"]["default"], "effortLevel": cfg["model"]["effort"], "env": build_env(cfg),
                 "permissions": {"deny": build_deny(cfg)}}
    if cfg["usage"]["scripts"]:
        out["permissions"]["allow"] = build_allow(cfg)
    hooks: dict = {}
    if cfg["hooks"]["trim_test_output"]:
        hooks["PreToolUse"] = [hook_entry(cfg)]
    if cfg["model"]["opus_reminder"]:
        hooks["UserPromptSubmit"] = [opus_hook_entry(cfg)]
    if stop_hook_on(cfg):
        hooks["Stop"] = [log_hook_entry(cfg)]
        hooks["SubagentStop"] = [log_hook_entry(cfg)]
    if config.telemetry_active(cfg):
        hooks["PreCompact"] = [compact_hook_entry(cfg)]
        if cfg["telemetry"]["auto_register"]:
            hooks["SessionStart"] = [session_hook_entry(cfg)]
    if hooks:
        out["hooks"] = hooks
    if cfg["monitoring"]["statusline"]:
        # plain command form (the documented statusLine shape); ${VAR:-.} falls back to the current directory
        out["statusLine"] = {"type": "command", "command": f'{cfg["hooks"]["python"]} "${{CLAUDE_PROJECT_DIR:-.}}/{STATUS_SCRIPT}"'}
    return out


def is_cckit_hook(entry: dict) -> bool:
    for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
        blob = json.dumps(h)
        if any(m in blob for m in HOOK_MARKERS):
            return True
    return False


# ----------------------------------------------------------------------------- hook scripts
def render_opus_script(cfg: dict) -> str:
    return asset("opus_reminder.py.tpl")


def render_trim_script(cfg: dict) -> str:
    return asset("trim_output.py.tpl").replace("__TAIL_LINES__", str(cfg["hooks"]["tail_lines"]))


def render_log_script(cfg: dict) -> str:
    t = cfg["telemetry"]
    return (asset("log_token_usage.py.tpl").replace("__CSV_PATH__", json.dumps(cfg["usage"]["csv_path"].replace("\\", "/")))
            .replace("__WRITE_CSV__", "True" if cfg["usage"]["log"] else "False")
            .replace("__STOP_REPORT__", "True" if config.stop_report_active(cfg) else "False")
            .replace("__ENDPOINT__", json.dumps(config.identity_key(t)))
            .replace("__DOMAINS__", json.dumps(list(t["allowed_domains"])))
            .replace("__USAGE_URL__", json.dumps(config.api_url(t, "usage")))
            .replace("__FLUSH_EVERY__", str(int(t["stop_interval"])))
            .replace("__WINDOW__", str(cfg["context"]["auto_compact_window"]))
            .replace("__SEND_PROJECT__", "True" if t["send_project"] else "False"))


def clean_prices(cfg: dict) -> dict:
    out = {}
    for word, p in (cfg.get("prices") or {}).items():
        if isinstance(p, dict):
            out[str(word)] = {k: float(p[k]) for k in ("input", "output", "cache_read", "cache_write") if isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool)}
    return out


def render_report_script(cfg: dict) -> str:
    return (asset("usage_report.py.tpl").replace("__PRICES__", json.dumps(clean_prices(cfg), indent=None))
            .replace("__WINDOW__", str(cfg["context"]["auto_compact_window"])).replace("__WARN_PCT__", str(cfg["monitoring"]["warn_context_pct"])))


def build_usage_command() -> str:
    return asset("usage_command.md")


def build_gitignore_block(cfg: dict) -> str | None:
    """Lines that keep the local usage log and the hook's state file out of git (None when the Stop hook is off)."""
    if not stop_hook_on(cfg):
        return None
    p = cfg["usage"]["csv_path"].replace("\\", "/")
    p = p[2:] if p.startswith("./") else p
    return f"{GITIGNORE_BEGIN}\n/{p}\n/{p}.state.json\n/{p}.lock\n{GITIGNORE_END}\n"


def upsert_gitignore(existing: str | None, block: str | None) -> str | None:
    """Insert, replace or (block=None) remove the managed block of .gitignore; everything outside the markers is kept. None = nothing to write."""
    text = existing or ""
    b, e = text.find("# cckit:begin"), text.find(GITIGNORE_END)
    has = b != -1 and e != -1 and e > b
    if block is None:
        if not has:
            return existing
        after = text[e + len(GITIGNORE_END):].lstrip("\n")
        return (text[:b].rstrip("\n") + ("\n" if text[:b].strip() else "") + after) or None
    if has:
        after = text[e + len(GITIGNORE_END):].lstrip("\n")
        return text[:b] + block + ("\n" + after if after else "")
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block


def render_compact_script(cfg: dict) -> str:
    t = cfg["telemetry"]
    return (asset("compact_report.py.tpl")
            .replace("__ENDPOINT__", json.dumps(config.identity_key(t)))
            .replace("__REGISTER_URL__", json.dumps(config.api_url(t, "register")))
            .replace("__USAGE_URL__", json.dumps(config.api_url(t, "usage")))
            .replace("__DOMAINS__", json.dumps(list(t["allowed_domains"])))
            .replace("__AUTO_REGISTER__", "True" if t["auto_register"] else "False")
            .replace("__TRIGGERS__", json.dumps(list(t["events"])))
            .replace("__WINDOW__", str(cfg["context"]["auto_compact_window"]))
            .replace("__SEND_PROJECT__", "True" if t["send_project"] else "False"))


def render_statusline_script(cfg: dict) -> str:
    return asset("statusline.py.tpl").replace("__WARN_PCT__", str(cfg["monitoring"]["warn_context_pct"]))


# ----------------------------------------------------------------------------- CLAUDE.md
def build_claude_md_block(cfg: dict) -> str:
    body = """\
# Working agreements (cost-aware)
- Be specific. Name the file or function you want changed; do not ask for "improve the codebase".
- For a change that touches more than ~3 files, plan first (Shift+Tab for plan mode) and agree the approach before editing.
- Keep noisy work out of the main conversation: use the `explorer` subagent to find code, `test-runner` to run tests,
  `reviewer` to review a diff. They return a short summary.
- Never paste or read whole logs or large generated files. Show the failing tests and the last ~50 lines.
- Do not re-read a file that is already in the conversation. Prefer grep or a line range over reading a large file.
- Run only the tests that cover the change. Run the full suite once, at the end.
- If an approach fails twice, stop and ask instead of trying a third variation.
- Between unrelated tasks run `/clear`. At a natural break in a long task run `/compact`.
"""
    if cfg["model"]["opus_reminder"]:
        body += """- If you are running on an Opus model, end every reply with one line starting "Model tip:" that says whether this task needed Opus; if a cheaper model would have done, name it and the `/model` command.
"""
    if cfg["usage"]["scripts"]:
        body += f"""- For tokens, cost or context used so far, run `{cfg["hooks"]["python"]} {REPORT_SCRIPT}` (add `--session all --days 7` for the week). Do not estimate them yourself.
"""
    if cfg["usage"]["log"]:
        body += f"""- `{cfg["usage"]["csv_path"]}` is a local log of every response's token counts. It grows without limit: never read it whole, use the report script.
"""
    body += """
# Compact instructions
When compacting, keep: the goal of the task, decisions made and why, files changed, failing tests with their error lines,
and open questions. Drop: tool output, exploration dead ends, and full file contents.
"""
    return f"{MARK_BEGIN}\n{body}{MARK_END}\n"


def upsert_block(existing: str | None, block: str) -> str:
    """Insert or replace the managed block, keeping everything outside the markers."""
    if not existing:
        return block
    b = existing.find("<!-- cckit:begin")
    e = existing.find(MARK_END)
    if (b == -1) != (e == -1) or (b != -1 and e < b):
        # a lone marker: replacing "from the first marker to the last" could delete the user's own text, so cckit does not guess
        raise ValueError("CLAUDE.md has a cckit marker without its partner (<!-- cckit:begin ... --> and <!-- cckit:end -->). "
                         "Fix or remove it by hand, then run `cckit init` again; nothing was changed.")
    if b != -1 and e != -1 and e > b:
        after = existing[e + len(MARK_END):].lstrip("\n")
        return existing[:b] + block + ("\n" + after if after else "")
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + block


# ----------------------------------------------------------------------------- rules and subagents
def build_rules(cfg: dict) -> dict[str, str]:
    testing = """\
---
paths:
  - "tests/**"
  - "**/test_*.py"
  - "**/*.test.*"
  - "**/*.spec.*"
---

# Working with tests
- Run only the tests for the code you changed (a file or a `-k` expression), not the whole suite.
- Use the `test-runner` subagent for long test runs. Report only failing tests and their first error lines.
- Do not paste full test output into the conversation.
"""
    return {".claude/rules/testing.md": testing}


AGENT_BODIES = {
    "explorer": (
        "Read-only code exploration. Use proactively to find where something is implemented, trace a call path, "
        "or summarise a module, instead of reading many files in the main conversation.",
        "Read, Grep, Glob",
        """\
You explore a codebase and report back. You never edit files.

How to work:
- Start with Grep or Glob to locate things; read only the line ranges you need.
- Stop as soon as you can answer the question.

Report format (keep it under 15 lines):
- The answer in one or two sentences.
- The relevant files as `path:line` with a few words each.
- Anything you could not find.
Do not paste code blocks longer than 10 lines.
""",
    ),
    "test-runner": (
        "Runs the test suite or a subset and reports only what failed. Use proactively for any test run that may print a lot.",
        "Bash, Read, Grep, Glob",
        """\
You run tests and report the result. You do not fix code and you do not edit files.

How to work:
- Run exactly the tests you were asked to run. If none were named, run the tests related to the changed files.
- If a run fails to start, report the error once and stop.

Report format (keep it under 40 lines):
- Passed / failed / skipped counts.
- For each failing test: its name and the first lines of its error (at most 8 lines).
- The likely cause in one sentence, only if it is obvious.
Never paste the full output.
""",
    ),
    "reviewer": (
        "Reviews a diff or a set of files for bugs, security problems and missing tests. Use proactively before a commit.",
        "Read, Grep, Glob",
        """\
You review code and report findings. You never edit files.

How to work:
- Review only what you were pointed at (a diff, a file list). Do not wander through the repository.
- Report real problems: bugs, security issues, missing error handling, missing tests. Skip style nitpicks.

Report format:
- One line per finding: `path:line - problem - suggested fix`.
- Highest severity first. At most 10 findings.
- If you find nothing important, say so in one sentence.
""",
    ),
}


def build_agents(cfg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, a in cfg.get("agents", {}).items():
        if a.get("enabled", True) is False:
            continue
        desc, tools, body = AGENT_BODIES.get(name, (a.get("description", f"Subagent {name}."), a.get("tools", "Read, Grep, Glob"), a.get("instructions", "Do the task and report briefly.\n")))
        lines = ["---", f"name: {name}", f"description: {json.dumps(a.get('description', desc))}", f"tools: {a.get('tools', tools)}",
                 f"model: {a.get('model', 'inherit')}"]
        if "max_turns" in a:
            lines.append(f"maxTurns: {a['max_turns']}")
        if "effort" in a:
            lines.append(f"effort: {a['effort']}")
        lines.append("---")
        out[f".claude/agents/{name}.md"] = "\n".join(lines) + "\n\n" + a.get("instructions", body)
    return out


# ----------------------------------------------------------------------------- MCP profiles
def build_mcp_profiles(cfg: dict) -> dict[str, str]:
    """`none` (no MCP servers at all) plus every profile from [mcp.profiles.*]."""
    out = {".cckit/mcp-profiles/none.json": json.dumps({"mcpServers": {}}, indent=2) + "\n"}
    for name, prof in cfg.get("mcp", {}).get("profiles", {}).items():
        out[f".cckit/mcp-profiles/{name}.json"] = json.dumps({"mcpServers": prof.get("servers", {})}, indent=2) + "\n"
    return out


# ----------------------------------------------------------------------------- the server side: a standalone app
USAGE_APP_FILES = {"README.md": "README.md", "Dockerfile": "Dockerfile", ".env.example": "env.example", ".gitignore": "gitignore", ".dockerignore": "dockerignore"}


def build_usage_app() -> dict[str, str]:
    """The deployable usage API: usage_api.py (a copy of cckit.server, one file, no dependencies), Dockerfile, README with the endpoint reference, env example."""
    files = {name: resources.files("cckit").joinpath("assets", "usage_app", src).read_text(encoding="utf-8") for name, src in USAGE_APP_FILES.items()}
    files["usage_api.py"] = resources.files("cckit").joinpath("server.py").read_text(encoding="utf-8")
    return dict(sorted(files.items()))


# ----------------------------------------------------------------------------- admin-only example
def build_managed_example(cfg: dict) -> str:
    """For an administrator: managed settings (these keys are NOT honoured from a repo settings file)."""
    mon = cfg["monitoring"]
    managed: dict = {"availableModels": cfg["managed"]["available_models"], "maxEffortLevel": cfg["managed"]["max_effort_level"]}
    endpoint = mon["otel_endpoint"] or "http://collector.example.com:4317"
    managed["env"] = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
    }
    note = ("Example for an administrator. Deploy it as managed settings (server-managed settings from the claude.ai admin console, "
            "an MDM policy or a managed-settings.json file). OTEL_* variables are ignored in a repository's .claude/settings.json. "
            "Metrics: claude_code.token.usage and claude_code.cost.usage. Per-person spend limits are set in the admin console, not in a file.")
    return json.dumps({"_comment": note, **managed}, indent=2) + "\n"


def build_readme(cfg: dict) -> str:
    return """\
# .cckit

Generated by `cckit init` from `.cckit/cckit.toml` (claude-cost-kit). Do not edit the generated files; edit `.cckit/cckit.toml` and run `cckit init` again.

| Path | What it is |
|---|---|
| `.cckit/cckit.toml` | the settings this repo is generated from (edit it, then run `cckit init`) |
| `.cckit/key.json` | optional: the team key `{"key": "..."}` for automatic registration. Commit it only in a private repository; Claude is not allowed to read it |
| `.claude/settings.json` | model, effort, compaction, subagent limits, tool search, hooks, status line, read blocks |
| `CLAUDE.md` | short working agreements between the `cckit` markers (your own text outside them is kept) |
| `.claude/agents/` | cheap, limited subagents: explorer, test-runner, reviewer |
| `.cckit/hooks/` | `opus_reminder.py` (a `Model tip:` line on every reply while an Opus model is in use), `trim_output.py` (shortens test output), `statusline.py`, `log_token_usage.py` (appends each response's token counts to `.cckit/token_usage.csv`, git-ignored), and `compact_report.py` (only with `[telemetry]` on: registers the developer (company e-mail from git) and reports each compaction to the team usage API) |
| `.cckit/scripts/`, `.claude/commands/` | `usage_report.py` and `/usage`: session usage and estimated cost, which Claude or you can run |
| `.claude/rules/` | path-scoped rules, loaded only when Claude touches matching files |
| `mcp-profiles/` | MCP server sets for `cckit run --profile NAME` |
| `managed-settings.example.json` | for an administrator: OpenTelemetry and model limits (not enforceable from a repo file) |

Commands: `cckit doctor` (check), `cckit register` (only if automatic registration found no company e-mail in git), `cckit whoami --check`, `cckit report` (token usage from local sessions), `cckit run --profile NAME` (start Claude Code with only the MCP servers for a task).
"""
