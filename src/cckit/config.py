"""cckit.toml: the single source of truth for what gets generated. Defaults live in DEFAULT_TOML."""
from __future__ import annotations

import copy
import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

CONFIG_NAME = "cckit.toml"

DEFAULT_TOML = '''\
# cckit.toml - source of truth for this repo's Claude Code cost settings.
# Edit this file, then run:   cckit init      (safe to re-run; it only touches what it owns)
# Check everything with:      cckit doctor

[model]
default = "sonnet"            # alias (sonnet | haiku | opus | fable) or a full model name. "sonnet" is Sonnet 5.5 on the
                              # Anthropic API; cloud providers map it to their own version, so pin a full name there.
effort = "high"               # low | medium | high | xhigh  (settings key effortLevel; developers can still use /effort)
subagent = "haiku"            # model for subagents that do not pick their own (env CLAUDE_CODE_SUBAGENT_MODEL)
subagent_force = false        # true = every subagent uses the model above, even if its file says otherwise
block_opus_subagents = true   # deny rule Agent(model:opus): subagents may not be started on Opus

[context]
auto_compact_window = 200000  # tokens, 100000..1000000 (env CLAUDE_CODE_AUTO_COMPACT_WINDOW)
auto_compact_pct = 50         # 1..100: compact when this % of the window is used. It can only LOWER the default.
cap_context_to_200k = true    # env CLAUDE_CODE_DISABLE_1M_CONTEXT=1 (newer models default to a 1M window)
claude_md_max_lines = 200     # `cckit doctor` fails when CLAUDE.md or a rules file is longer

[thinking]
max_tokens = 0                # 0 = leave unset. Only works on models with a FIXED thinking budget.
fixed_budget = false          # env CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1 (Opus/Sonnet 4.6 only; no effect on Sonnet 5+)

[subagents]
max_concurrent = 4            # env CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS (Claude Code default: 20)
max_depth = 1                 # env CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH (1 = subagents cannot start subagents; default 3)

[agents.explorer]             # read-only code exploration   (add `enabled = false` to drop an agent)
model = "haiku"
max_turns = 15
effort = "low"

[agents.test-runner]          # runs tests, returns only the failures
model = "haiku"
max_turns = 10
effort = "low"

[agents.reviewer]             # read-only review
model = "sonnet"
max_turns = 20
effort = "medium"

[tools]
tool_search = "true"          # ENABLE_TOOL_SEARCH: true (only tool NAMES load up front) | auto | auto:N | false
bash_max_output_chars = 20000 # env BASH_MAX_OUTPUT_LENGTH (default 30000, max 150000)
mcp_max_output_tokens = 15000 # env MAX_MCP_OUTPUT_TOKENS (default 25000)

[hooks]
python = "python3"            # interpreter that runs the hook scripts; use "python" on Windows
trim_test_output = true       # keep only the last lines of test runner output (pytest, npm test, go test ...)
tail_lines = 100

[permissions]
# Files Claude must not read (keeps big or generated files out of the context). Gitignore-style patterns.
deny_reads = ["node_modules/**", ".venv/**", "venv/**", "dist/**", "build/**", "**/__pycache__/**", "**/*.min.js", "**/*.map"]

[monitoring]
statusline = true             # show model, context % and session cost at the bottom of Claude Code
warn_context_pct = 60         # the status line turns into a "/compact or /clear" warning above this
otel_endpoint = ""            # set to your collector (for example http://collector.example.com:4317) to get OpenTelemetry
                              # settings in .cckit/managed-settings.example.json (they are IGNORED in a repo settings file)

[usage]
log = true                    # a Stop hook appends the token counts of every response to csv_path (numbers only; local; git-ignored)
csv_path = ".claude/token_usage.csv"
scripts = true                # install usage_report.py and the /usage command: Claude or you can run them for session usage and estimated cost

[telemetry]                   # report every context compaction to the team usage app (see `cckit register`). false = nothing is registered or sent
enabled = true
endpoint = ""                 # OR one base URL: then register is <endpoint>?api=register and usage is <endpoint>?api=usage
register_url = "https://claude-code-usage.hatchable.site/api/register"   # each API's own URL; these win over endpoint. To move to another app or
usage_url = "https://claude-code-usage.hatchable.site/api/usage"         # domain edit them, run `cckit init` and commit (machines then register again)
ping_url = ""                 # optional health check for `cckit whoami --check` (default: .../api/ping next to register_url)
allowed_domains = ["tigeranalytics.com"]   # registration needs an e-mail on one of these domains (checked here and again on the server)
team_key = ""                 # join code for `cckit register` (ask your admin, or set CCKIT_TEAM_KEY). It is not shipped with cckit. If you put it here it is
                              # committed with the repo: do that only for a PRIVATE repo, and only if you want teammates to be registered automatically
auto_register = true          # a SessionStart hook takes the developer's company e-mail from `git config user.email` (plus their GitHub login,
                              # if it finds one) and registers them; if there is no company e-mail it asks Claude to ask the developer.
                              # Needs team_key. false = developers run `cckit register`
events = ["auto"]             # which compactions to report: "auto", "manual"
stop_report = true            # the Stop hook also reports each turn to the team app: tokens per model, current model and effort, tools used, how long it took.
                              # false = only the local CSV
stop_interval = 0             # seconds: at least this long between two reports (0 = one report per turn). Raise it if the app's hourly limit is reached
send_project = true           # include the repo folder name (never paths, prompts, code or summaries)

[managed]                     # only used for .cckit/managed-settings.example.json (an admin deploys that file)
available_models = ["sonnet", "haiku"]
max_effort_level = "high"

# MCP servers per task. `cckit run --profile docs` starts Claude Code with ONLY these servers.
# Without a profile, Claude Code loads whatever .mcp.json and your user settings define.
# [mcp.profiles.docs.servers.my-docs]
# type = "http"
# url = "https://example.com/mcp"

# Optional: price per million tokens, so `cckit report` and usage_report.py can estimate cost. Match by a word in the model name.
# Use the rates of YOUR agreement; none are built in because they change.
# [prices.sonnet]
# input = 3.0
# output = 15.0
# cache_read = 0.3
# cache_write = 3.75
'''

EFFORTS = ("low", "medium", "high", "xhigh")
MODEL_ALIASES = ("sonnet", "haiku", "opus", "fable")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def defaults() -> dict:
    return tomllib.loads(DEFAULT_TOML)


def load(root: Path) -> dict:
    """Defaults overlaid with <root>/cckit.toml (when present)."""
    cfg = defaults()
    path = Path(root) / CONFIG_NAME
    if path.exists():
        try:
            user = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{CONFIG_NAME} is not valid TOML: {e}") from e
        cfg = _merge(cfg, user)
        ut = user.get("telemetry") if isinstance(user.get("telemetry"), dict) else {}
        if ut.get("endpoint"):
            # The user chose their own base URL: the shipped default register_url / usage_url / ping_url must not silently win over it.
            for k in ("register_url", "usage_url", "ping_url"):
                if k not in ut:
                    cfg["telemetry"][k] = ""
    return cfg


def valid_endpoint(url: str) -> bool:
    """https only, so tokens never travel in clear text. Plain http is accepted for a local test server."""
    u = urlparse(url)
    if not u.hostname:
        return False
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))


def api_url(t: dict, name: str) -> str:
    """URL of one API (register | usage | ping): the explicit [telemetry] <name>_url, else <endpoint>?api=<name>, else (for ping) the sibling of
    register_url / usage_url (.../api/register -> .../api/ping). Empty when nothing is configured."""
    if t.get(f"{name}_url"):
        return t[f"{name}_url"]
    base = t.get("endpoint") or ""
    if base:
        return base + ("&" if "?" in base else "?") + "api=" + name
    if name == "ping":
        for k in ("register_url", "usage_url"):
            u = t.get(k) or ""
            for suffix in ("/register", "/usage"):
                if u.endswith(suffix):
                    return u[: -len(suffix)] + "/ping"
    return ""


def identity_key(t: dict) -> str:
    """What the per-machine token is filed under: the endpoint, or the usage URL when there is no endpoint. A new app or domain means a new key."""
    return t.get("endpoint") or t.get("usage_url") or ""


def stop_report_active(cfg: dict) -> bool:
    """The Stop hook posts token counts to the usage API."""
    return bool(telemetry_active(cfg) and cfg["telemetry"]["stop_report"])


def telemetry_active(cfg: dict) -> bool:
    t = cfg["telemetry"]
    return bool(t["enabled"] and api_url(t, "register") and api_url(t, "usage"))


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(cfg: dict) -> list[str]:
    """Return a list of problems (empty = valid)."""
    errs: list[str] = []
    m, c, t, s, tl, h = (cfg["model"], cfg["context"], cfg["thinking"], cfg["subagents"], cfg["tools"], cfg["hooks"])
    if not isinstance(m.get("default"), str) or not m["default"].strip():
        errs.append("[model] default must be a non-empty string")
    if m.get("effort") not in EFFORTS:
        errs.append(f"[model] effort must be one of {', '.join(EFFORTS)} ('max' cannot be set in settings.json)")
    if not isinstance(m.get("subagent"), str) or not m["subagent"].strip():
        errs.append("[model] subagent must be a non-empty string")
    for k in ("subagent_force", "block_opus_subagents"):
        if not isinstance(m.get(k), bool):
            errs.append(f"[model] {k} must be true or false")
    w = c.get("auto_compact_window")
    if not _is_int(w) or not 100_000 <= w <= 1_000_000:
        errs.append("[context] auto_compact_window must be an integer from 100000 to 1000000")
    p = c.get("auto_compact_pct")
    if not _is_int(p) or not 1 <= p <= 100:
        errs.append("[context] auto_compact_pct must be an integer from 1 to 100")
    if not isinstance(c.get("cap_context_to_200k"), bool):
        errs.append("[context] cap_context_to_200k must be true or false")
    if not _is_int(c.get("claude_md_max_lines")) or c["claude_md_max_lines"] < 20:
        errs.append("[context] claude_md_max_lines must be an integer >= 20")
    if not _is_int(t.get("max_tokens")) or t["max_tokens"] < 0:
        errs.append("[thinking] max_tokens must be 0 (unset) or a positive integer")
    if not isinstance(t.get("fixed_budget"), bool):
        errs.append("[thinking] fixed_budget must be true or false")
    for k in ("max_concurrent", "max_depth"):
        if not _is_int(s.get(k)) or s[k] < 1:
            errs.append(f"[subagents] {k} must be a positive integer")
    ts = str(tl.get("tool_search"))
    if not (ts in ("true", "false", "auto") or re.fullmatch(r"auto:(\d{1,3})", ts) and int(ts.split(":")[1]) <= 100):
        errs.append("[tools] tool_search must be true, false, auto or auto:N (N = 0..100)")
    bo = tl.get("bash_max_output_chars")
    if not _is_int(bo) or not 1000 <= bo <= 150_000:
        errs.append("[tools] bash_max_output_chars must be an integer from 1000 to 150000")
    if not _is_int(tl.get("mcp_max_output_tokens")) or tl["mcp_max_output_tokens"] < 1000:
        errs.append("[tools] mcp_max_output_tokens must be an integer >= 1000")
    if not isinstance(h.get("python"), str) or not h["python"].strip():
        errs.append("[hooks] python must be the interpreter name, e.g. python3")
    if not isinstance(h.get("trim_test_output"), bool):
        errs.append("[hooks] trim_test_output must be true or false")
    if not _is_int(h.get("tail_lines")) or not 10 <= h["tail_lines"] <= 2000:
        errs.append("[hooks] tail_lines must be an integer from 10 to 2000")
    dr = cfg["permissions"].get("deny_reads")
    if not isinstance(dr, list) or not all(isinstance(x, str) and x.strip() for x in dr):
        errs.append("[permissions] deny_reads must be a list of path patterns")
    mon = cfg["monitoring"]
    if not isinstance(mon.get("statusline"), bool):
        errs.append("[monitoring] statusline must be true or false")
    if not _is_int(mon.get("warn_context_pct")) or not 1 <= mon["warn_context_pct"] <= 100:
        errs.append("[monitoring] warn_context_pct must be an integer from 1 to 100")
    if not isinstance(mon.get("otel_endpoint"), str):
        errs.append("[monitoring] otel_endpoint must be a string")
    us = cfg["usage"]
    for k in ("log", "scripts"):
        if not isinstance(us.get(k), bool):
            errs.append(f"[usage] {k} must be true or false")
    cp = us.get("csv_path")
    if not isinstance(cp, str) or not cp.strip() or cp.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", cp) or ".." in cp.replace("\\", "/").split("/"):
        errs.append("[usage] csv_path must be a relative path inside the repository, for example .claude/token_usage.csv")
    tel = cfg["telemetry"]
    for k in ("enabled", "send_project", "auto_register"):
        if not isinstance(tel.get(k), bool):
            errs.append(f"[telemetry] {k} must be true or false")
    if not isinstance(tel.get("team_key"), str):
        errs.append("[telemetry] team_key must be a string")
    for k in ("endpoint", "register_url", "usage_url", "ping_url"):
        if not isinstance(tel.get(k), str) or (tel[k] and not valid_endpoint(tel[k])):
            errs.append(f"[telemetry] {k} must be an https:// URL (http:// is allowed only for localhost)")
    doms = tel.get("allowed_domains")
    if not isinstance(doms, list) or not doms or not all(isinstance(d, str) and re.fullmatch(r"[A-Za-z0-9.\-]{3,60}", d) and "@" not in d for d in doms):
        errs.append('[telemetry] allowed_domains must be a non-empty list of domains such as ["tigeranalytics.com"]')
    ev = tel.get("events")
    if not isinstance(ev, list) or not ev or not set(ev) <= {"auto", "manual"}:
        errs.append('[telemetry] events must be a non-empty list of "auto" and/or "manual"')
    if not isinstance(tel.get("stop_report"), bool):
        errs.append("[telemetry] stop_report must be true or false")
    if not _is_int(tel.get("stop_interval")) or not 0 <= tel["stop_interval"] <= 3600:
        errs.append("[telemetry] stop_interval must be a whole number of seconds from 0 to 3600")
    for name, a in cfg.get("agents", {}).items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            errs.append(f"[agents.{name}] the name must be lower-case letters, digits and hyphens")
        if not isinstance(a.get("model", "inherit"), str):
            errs.append(f"[agents.{name}] model must be a string")
        if "enabled" in a and not isinstance(a["enabled"], bool):
            errs.append(f"[agents.{name}] enabled must be true or false")
        if "max_turns" in a and (not _is_int(a["max_turns"]) or a["max_turns"] < 1):
            errs.append(f"[agents.{name}] max_turns must be a positive integer")
        if "effort" in a and a["effort"] not in ("low", "medium", "high", "xhigh", "max"):
            errs.append(f"[agents.{name}] effort must be low, medium, high, xhigh or max")
    for pname, prof in cfg.get("mcp", {}).get("profiles", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", pname):
            errs.append(f"[mcp.profiles.{pname}] the name may only use letters, digits, - and _")
        if not isinstance(prof.get("servers", {}), dict):
            errs.append(f"[mcp.profiles.{pname}] servers must be a table of servers")
    return errs
