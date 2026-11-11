"""`cckit doctor`: check that the repo really has the settings .cckit/cckit.toml asks for, and flag known pitfalls."""
from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import apply, config, identity, render

# Documented Claude Code subagent frontmatter values (https://code.claude.com/docs/en/sub-agents)
AGENT_MODELS = {"sonnet", "opus", "haiku", "fable", "inherit"}


@dataclass
class Report:
    items: list[tuple[str, str]] = field(default_factory=list)   # (level, text) level: ok | warn | fail | info

    def add(self, level: str, text: str) -> None:
        self.items.append((level, text))

    @property
    def failed(self) -> bool:
        return any(lv == "fail" for lv, _ in self.items)

    def render(self) -> str:
        tag = {"ok": "ok   ", "warn": "WARN ", "fail": "FAIL ", "info": "info "}
        out = [f"{tag[lv]} {t}" for lv, t in self.items]
        n = {k: sum(1 for lv, _ in self.items if lv == k) for k in ("ok", "warn", "fail")}
        out.append(f"\n{n['ok']} ok, {n['warn']} warnings, {n['fail']} failures")
        return "\n".join(out)


def _version_tuple(text: str):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def claude_version() -> tuple[int, int, int] | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    return _version_tuple(out)


def _count_lines(p: Path) -> int:
    return len(p.read_text(encoding="utf-8").splitlines())


def _frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fm: dict[str, str] = {}
    for ln in text[3:end].splitlines():
        if ":" in ln and not ln.startswith((" ", "\t", "-")):
            k, v = ln.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def run(root: Path, check_claude: bool = True) -> Report:
    root = Path(root)
    r = Report()

    # 1. config
    try:
        cfg = config.load(root)
    except ValueError as e:
        r.add("fail", str(e))
        return r
    errs = config.validate(cfg)
    for e in errs:
        r.add("fail", e)
    if errs:
        return r
    cfg_file = config.config_path(root)
    r.add("ok", f"{cfg_file.relative_to(root).as_posix()} is valid" + ("" if cfg_file.exists() else " (defaults; file not created yet, run `cckit init`)"))
    if cfg_file.name == config.LEGACY_CONFIG_NAME and cfg_file.parent == Path(root):
        r.add("warn", f"the config is in the old place ({config.LEGACY_CONFIG_NAME}); run `cckit init` to move it to {config.CONFIG_NAME}")

    # 2. settings.json against the config
    sp = root / apply.SETTINGS
    if not sp.exists():
        r.add("fail", f"{apply.SETTINGS} is missing. Run `cckit init`.")
    else:
        try:
            s = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            r.add("fail", f"{apply.SETTINGS} is not valid JSON: {e}")
            s = None
        if isinstance(s, dict):
            want = render.build_settings(cfg)
            wrong, missing = [], []
            for k, v in want["env"].items():
                have = (s.get("env") or {}).get(k)
                if have is None:
                    missing.append(k)
                elif have != v:
                    wrong.append(f"{k}={have} (.cckit/cckit.toml says {v})")
            for k in ("model", "effortLevel"):
                if s.get(k) != want[k]:
                    wrong.append(f"{k}={s.get(k)!r} (.cckit/cckit.toml says {want[k]!r})")
            for rule in want["permissions"]["deny"]:
                if rule not in ((s.get("permissions") or {}).get("deny") or []):
                    missing.append(f"deny {rule}")
            for rule in want["permissions"].get("allow", []):
                if rule not in ((s.get("permissions") or {}).get("allow") or []):
                    missing.append(f"allow {rule}")
            for ev in (want.get("hooks") or {}):
                if not any(render.is_cckit_hook(e) for e in ((s.get("hooks") or {}).get(ev) or [])):
                    missing.append(f"{ev} hook")
            if "statusLine" in want and s.get("statusLine") != want["statusLine"]:
                missing.append("statusLine")
            if missing:
                r.add("fail", "settings.json is missing: " + ", ".join(missing) + ". Run `cckit init`.")
            if wrong:
                r.add("warn", "settings.json differs from .cckit/cckit.toml: " + "; ".join(wrong))
            if not missing and not wrong:
                r.add("ok", "settings.json matches .cckit/cckit.toml")
            leaked = [k for k in (s.get("env") or {}) if k.startswith("OTEL_") or k == "CLAUDE_CODE_ENABLE_TELEMETRY"]
            if leaked:
                r.add("warn", "OpenTelemetry variables in a repo settings file are ignored by Claude Code; put them in managed or user settings: " + ", ".join(leaked))

    # 3. instruction files stay short (every line is paid for on every turn)
    limit = cfg["context"]["claude_md_max_lines"]
    cm = apply.claude_md_path(root)
    if not cm.exists():
        r.add("fail", "CLAUDE.md is missing. Run `cckit init`.")
    else:
        n = _count_lines(cm)
        r.add("ok" if n <= limit else "fail", f"CLAUDE.md has {n} lines (limit {limit})" + ("" if n <= limit else ": move detail into .claude/rules/ or a skill"))
        txt = cm.read_text(encoding="utf-8")
        if render.MARK_END not in txt:
            r.add("warn", "CLAUDE.md has no cckit block (the cost-aware working agreements). Run `cckit init`.")
    rules = sorted((root / ".claude" / "rules").glob("**/*.md")) if (root / ".claude" / "rules").exists() else []
    for p in rules:
        n = _count_lines(p)
        if n > limit:
            r.add("fail", f"{p.relative_to(root)} has {n} lines (limit {limit})")
    for p in sorted(root.glob("**/CLAUDE.md")):
        if p == cm or any(part in (".git", "node_modules", ".venv") for part in p.parts):
            continue
        n = _count_lines(p)
        if n > limit:
            r.add("fail", f"{p.relative_to(root)} has {n} lines (limit {limit})")

    # 4. subagent files
    agents_dir = root / ".claude" / "agents"
    active = [n for n, a in cfg.get("agents", {}).items() if a.get("enabled", True) is not False]
    for name in active:
        p = agents_dir / f"{name}.md"
        if not p.exists():
            r.add("fail", f".claude/agents/{name}.md is missing. Run `cckit init`.")
            continue
        fm = _frontmatter(p.read_text(encoding="utf-8"))
        if not fm or "name" not in fm or "description" not in fm:
            r.add("fail", f".claude/agents/{name}.md needs `name` and `description` in its frontmatter")
            continue
        model = fm.get("model", "inherit")
        if model not in AGENT_MODELS and not model.startswith("claude-"):
            r.add("warn", f"agent {name}: model '{model}' is not an alias or a full model name")
        if model == "opus" and cfg["model"]["block_opus_subagents"]:
            r.add("warn", f"agent {name} uses opus, but block_opus_subagents is on")
        if "maxTurns" not in fm:
            r.add("warn", f"agent {name} has no maxTurns: it can run for a long time")
    if active:
        r.add("ok", f"{len(active)} subagent definitions checked")

    # 5. hook scripts and interpreter
    scripts = []
    if cfg["hooks"]["trim_test_output"]:
        scripts.append(render.TRIM_SCRIPT)
    if cfg["model"]["opus_reminder"]:
        scripts.append(render.OPUS_SCRIPT)
    if cfg["monitoring"]["statusline"]:
        scripts.append(render.STATUS_SCRIPT)
    if config.telemetry_active(cfg):
        scripts.append(render.COMPACT_SCRIPT)
    if render.stop_hook_on(cfg):
        scripts.append(render.LOG_SCRIPT)
    if cfg["usage"]["scripts"]:
        scripts += [render.REPORT_SCRIPT, render.USAGE_COMMAND]
    for rel in scripts:
        if not (root / rel).exists():
            r.add("fail", f"{rel} is missing. Run `cckit init`.")
    if scripts:
        py = cfg["hooks"]["python"]
        if shutil.which(py):
            r.add("ok", f"hook interpreter '{py}' found")
        else:
            r.add("warn", f"hook interpreter '{py}' is not on this machine's PATH. Hooks will not run here (set [hooks] python, e.g. 'python' on Windows).")

    # 5a. the local usage log must not be committed
    if cfg["usage"]["log"]:
        gi = root / ".gitignore"
        if gi.exists() and render.GITIGNORE_BEGIN.split(" - ")[0] in gi.read_text(encoding="utf-8"):
            csv_path = root / cfg["usage"]["csv_path"]
            n = max(0, len(csv_path.read_text(encoding="utf-8").splitlines()) - 1) if csv_path.exists() else 0
            r.add("ok", f"{cfg['usage']['csv_path']} is git-ignored" + (f" ({n} response(s) logged so far)" if csv_path.exists() else " (created at the first response)"))
        else:
            r.add("warn", f".gitignore has no cckit block, so {cfg['usage']['csv_path']} could be committed. Run `cckit init`.")

    # 5b. compaction reporting
    tel = cfg["telemetry"]
    if tel["enabled"] and not config.telemetry_active(cfg):
        r.add("warn", "[telemetry] is enabled but no URL is set, so nothing is reported. Set `endpoint`, or both `register_url` and `usage_url`, to the deployed usage app "
                      "(`cckit server export-app` writes one, with a README).")
    elif config.telemetry_active(cfg):
        if tel["auto_register"] and not tel["team_key"] and not os.environ.get("CCKIT_TEAM_KEY"):
            r.add("warn", f"auto_register needs the team key: put it in {config.KEY_NAME} as " + '{"key": "..."}' + " (or set CCKIT_TEAM_KEY); without it developers register with `cckit register`")
        elif tel["team_key"]:
            r.add("info", f"the team key is in {config.KEY_NAME}: commit that file only in a private repository (add it to .gitignore otherwise)")
        who, git_email = identity.pick_identity(config.identity_key(tel), str(root), tel["allowed_domains"])
        if who and git_email and identity.normalise_email(who["email"]) != git_email:
            r.add("warn", f"this machine is registered as {who['email']}, but this repo's git e-mail is {git_email}, so reports go out as {who['email']}. "
                          f"Run `cckit register` to register {git_email} (or fix `git config user.email`).")
        elif who:
            r.add("ok", f"compaction reports on; this machine is registered as {who['email']}")
        else:
            found = identity.detect_email(str(root), tel["allowed_domains"])
            how = f"found {found[0]} from {found[1]}; the SessionStart hook will register it" if found and tel["auto_register"] and (tel["team_key"] or os.environ.get("CCKIT_TEAM_KEY")) else "run `cckit register`"
            r.add("warn", f"compaction reports are on, but this machine is not registered yet ({how}).")

    # 6. things that quietly do nothing
    t = cfg["thinking"]
    if t["max_tokens"] and not t["fixed_budget"]:
        r.add("warn", "[thinking] max_tokens is ignored: Sonnet 5+, Haiku 5.5 and Opus 4.7+ always use adaptive reasoning. Use [model] effort instead, or set fixed_budget = true for Opus/Sonnet 4.6.")
    if cfg["context"]["auto_compact_pct"] > 80:
        r.add("warn", "[context] auto_compact_pct above 80 saves little; 50 to 70 keeps long sessions cheaper")
    if cfg["model"]["effort"] in ("high", "xhigh"):
        r.add("info", f"effort is '{cfg['model']['effort']}': more thinking tokens per request than the model default (Sonnet 5.5 defaults to medium). Developers can lower it per task with /effort.")
    if cfg["model"]["default"] in config.MODEL_ALIASES:
        r.add("info", f"model '{cfg['model']['default']}' is an alias: its version depends on how your developers connect (Anthropic API vs a cloud provider). Pin a full name if they differ.")
    if cfg["tools"]["tool_search"] == "false":
        r.add("warn", "[tools] tool_search = false loads every MCP tool definition up front")

    # 7. Claude Code version
    if check_claude:
        v = claude_version()
        if v is None:
            r.add("info", "`claude` not found on PATH; version checks skipped")
        else:
            r.add("ok", f"Claude Code {'.'.join(map(str, v))}")
            need = {"subagent limits (CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS, ..._SPAWN_DEPTH)": (2, 1, 217)}
            if cfg["model"]["subagent_force"]:
                need["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] = (2, 1, 257)
            for what, minv in need.items():
                if v < minv:
                    r.add("warn", f"{what} needs Claude Code {'.'.join(map(str, minv))} or later")
    return r
