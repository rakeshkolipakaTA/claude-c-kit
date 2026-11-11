"""`cckit init`: write the generated files into a repo without clobbering anyone's work.

Rules:
  * files cckit owns (agents, hooks, rules, MCP profiles, examples) are created, and updated only while
    nobody has edited them (a hash in .cckit/manifest.json says what cckit last wrote); an edited file is kept
    unless --force;
  * CLAUDE.md: only the text between the cckit markers is managed, the rest is yours;
  * .claude/settings.json: keys are merged. Values you set yourself win unless --force, lists are unioned, and what
    cckit added earlier is removed first so a changed cckit.toml does not leave stale entries behind.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import config, render

MANIFEST = ".cckit/manifest.json"
SETTINGS = ".claude/settings.json"
CLAUDE_MD = "CLAUDE.md"
EXECUTABLE = {render.TRIM_SCRIPT, render.STATUS_SCRIPT, render.COMPACT_SCRIPT, render.LOG_SCRIPT, render.REPORT_SCRIPT}


def claude_md_path(root: Path) -> Path:
    """./CLAUDE.md, or ./.claude/CLAUDE.md when that is the one the repository already uses (both are valid project memory files)."""
    root = Path(root)
    alt = root / ".claude" / "CLAUDE.md"
    return alt if not (root / CLAUDE_MD).exists() and alt.exists() else root / CLAUDE_MD


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Result:
    lines: list[str] = field(default_factory=list)
    changed: int = 0

    def add(self, kind: str, path: str, why: str = "") -> None:
        self.lines.append(f"{kind:<10} {path}" + (f"  ({why})" if why else ""))
        if kind in ("created", "updated", "merged"):
            self.changed += 1


def desired_files(cfg: dict) -> dict[str, str]:
    files: dict[str, str] = {}
    files.update(render.build_agents(cfg))
    files.update(render.build_rules(cfg))
    files.update(render.build_mcp_profiles(cfg))
    files[".cckit/managed-settings.example.json"] = render.build_managed_example(cfg)
    files[".cckit/README.md"] = render.build_readme(cfg)
    if cfg["hooks"]["trim_test_output"]:
        files[render.TRIM_SCRIPT] = render.render_trim_script(cfg)
    if cfg["monitoring"]["statusline"]:
        files[render.STATUS_SCRIPT] = render.render_statusline_script(cfg)
    if config.telemetry_active(cfg):
        files[render.COMPACT_SCRIPT] = render.render_compact_script(cfg)
    if cfg["usage"]["log"]:
        files[render.LOG_SCRIPT] = render.render_log_script(cfg)
    if cfg["usage"]["scripts"]:
        files[render.REPORT_SCRIPT] = render.render_report_script(cfg)
        files[render.USAGE_COMMAND] = render.build_usage_command()
    return files


def load_manifest(root: Path) -> dict:
    p = root / MANIFEST
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"version": 1, "files": {}, "settings": {}}


# ----------------------------------------------------------------------------- settings.json merge
def merge_settings(existing: dict, desired: dict, prev: dict, force: bool) -> tuple[dict, dict, list[str]]:
    """Return (new settings, what cckit now owns, notes). Pure function."""
    s = copy.deepcopy(existing)
    notes: list[str] = []

    # 1. take back what cckit added last time (only if the user has not changed it since)
    for k, v in (prev.get("env") or {}).items():
        if isinstance(s.get("env"), dict) and s["env"].get(k) == v:
            del s["env"][k]
    allow_prev = set(prev.get("allow") or [])
    if allow_prev and isinstance(s.get("permissions"), dict) and isinstance(s["permissions"].get("allow"), list):
        s["permissions"]["allow"] = [a for a in s["permissions"]["allow"] if a not in allow_prev]
        if not s["permissions"]["allow"]:
            del s["permissions"]["allow"]
    deny_prev = set(prev.get("deny") or [])
    if deny_prev and isinstance(s.get("permissions"), dict) and isinstance(s["permissions"].get("deny"), list):
        s["permissions"]["deny"] = [d for d in s["permissions"]["deny"] if d not in deny_prev]
    for k, v in (prev.get("scalars") or {}).items():
        if s.get(k) == v:
            del s[k]
    if prev.get("hook") or prev.get("hooks"):
        for ev in list((s.get("hooks") or {}).keys()):
            if isinstance(s["hooks"][ev], list):
                s["hooks"][ev] = [e for e in s["hooks"][ev] if not render.is_cckit_hook(e)]
                if not s["hooks"][ev]:
                    del s["hooks"][ev]
        if not s["hooks"]:
            del s["hooks"]

    owned: dict = {"env": {}, "deny": [], "allow": [], "scalars": {}, "hooks": []}

    # 2. scalar keys
    for k in ("model", "effortLevel", "statusLine"):
        if k not in desired:
            continue
        if k not in s or force:
            s[k] = desired[k]
            owned["scalars"][k] = desired[k]
        elif s[k] == desired[k]:
            owned["scalars"][k] = desired[k]
        else:
            notes.append(f"kept your '{k}' ({json.dumps(s[k])}); cckit.toml wants {json.dumps(desired[k])}. Use --force to replace it.")

    # 3. environment variables
    env = s.setdefault("env", {})
    for k, v in desired["env"].items():
        if k not in env or force:
            env[k] = v
            owned["env"][k] = v
        elif env[k] == v:
            owned["env"][k] = v
        else:
            notes.append(f"kept your env {k}={env[k]!r}; cckit.toml wants {v!r}. Use --force to replace it.")
    if not env:
        del s["env"]

    # 4. permission rules (union)
    perms = s.setdefault("permissions", {})
    deny = perms.setdefault("deny", [])
    for rule in desired["permissions"]["deny"]:
        if rule not in deny:
            deny.append(rule)
            owned["deny"].append(rule)

    want_allow = desired["permissions"].get("allow") or []
    if want_allow:
        allow = perms.setdefault("allow", [])
        for rule in want_allow:
            if rule not in allow:
                allow.append(rule)
                owned["allow"].append(rule)
    if not deny:
        del perms["deny"]
    if not perms:
        del s["permissions"]

    # 5. our hooks (PreToolUse: trim test output, PreCompact: report compactions)
    for ev, entries in (desired.get("hooks") or {}).items():
        hooks = s.setdefault("hooks", {})
        lst = hooks.setdefault(ev, [])
        lst[:] = [e for e in lst if not render.is_cckit_hook(e)]
        lst.append(entries[0])
        owned["hooks"].append(ev)
    return s, owned, notes


def _write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o755)


def init(root: Path, force: bool = False, dry_run: bool = False) -> Result:
    root = Path(root)
    res = Result()
    cfg_path = root / config.CONFIG_NAME
    cfg = config.load(root)                                  # defaults when cckit.toml does not exist yet
    errors = config.validate(cfg)
    if errors:
        raise ValueError("cckit.toml has problems:\n  - " + "\n  - ".join(errors))

    # Preflight: everything that can make init refuse is checked BEFORE the first file is written, so a refusal changes nothing at all.
    cm = claude_md_path(root)
    raw_md = cm.read_bytes().decode("utf-8") if cm.exists() else None
    crlf = raw_md is not None and "\r\n" in raw_md
    old_md = raw_md.replace("\r\n", "\n") if raw_md is not None else None
    new_md = render.upsert_block(old_md, render.build_claude_md_block(cfg))            # ValueError on a lone cckit marker
    sp = root / SETTINGS
    existing: dict = {}
    if sp.exists():
        try:
            existing = json.loads(sp.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"{SETTINGS} is not valid JSON ({e}). Fix it first; cckit will not overwrite it, and nothing was changed.") from e
        if not isinstance(existing, dict):
            raise ValueError(f"{SETTINGS} must contain a JSON object; nothing was changed.")

    if not cfg_path.exists():
        res.add("created", config.CONFIG_NAME, "defaults; edit it and run init again")
        if not dry_run:
            _write(cfg_path, config.DEFAULT_TOML)

    manifest = load_manifest(root)
    new_files: dict[str, str] = {}

    # owned files
    for rel, content in desired_files(cfg).items():
        p = root / rel
        if not p.exists():
            res.add("created", rel)
        else:
            cur = p.read_text(encoding="utf-8")
            if cur == content:
                res.add("unchanged", rel)
                new_files[rel] = sha(content)
                continue
            if manifest["files"].get(rel) == sha(cur) or force:
                res.add("updated", rel)
            else:
                res.add("kept", rel, "you edited it; use --force to overwrite")
                new_files[rel] = manifest["files"].get(rel, sha(cur))
                continue
        new_files[rel] = sha(content)
        if not dry_run:
            _write(p, content, rel in EXECUTABLE)

    # stale files that cckit wrote earlier but no longer wants (e.g. a removed agent): remove if untouched
    for rel, h in list(manifest["files"].items()):
        if rel in new_files:
            continue
        p = root / rel
        if p.exists() and sha(p.read_text(encoding="utf-8")) == h:
            res.add("removed", rel, "no longer in cckit.toml")
            if not dry_run:
                p.unlink()
        elif p.exists():
            res.add("kept", rel, "no longer generated, but you edited it")

    # CLAUDE.md managed block (the rest of the file is the user's and is never touched, line endings included)
    rel_cm = str(cm.relative_to(root)).replace("\\", "/")
    if old_md is None:
        res.add("created", rel_cm)
    elif old_md == new_md:
        res.add("unchanged", rel_cm)
    else:
        res.add("merged", rel_cm, "managed block only; your text is kept")
    if old_md != new_md and not dry_run:
        cm.parent.mkdir(parents=True, exist_ok=True)
        cm.write_bytes((new_md.replace("\n", "\r\n") if crlf else new_md).encode("utf-8"))

    # .gitignore: keep the local usage log out of git (only the marked block is ours)
    gi = root / ".gitignore"
    old_gi = gi.read_text(encoding="utf-8") if gi.exists() else None
    new_gi = render.upsert_gitignore(old_gi, render.build_gitignore_block(cfg))
    if new_gi != old_gi and not (new_gi is None and old_gi is None):
        res.add("created" if old_gi is None else "merged", ".gitignore", "managed block only" if old_gi is not None else "")
        if not dry_run:
            if new_gi:
                _write(gi, new_gi)
            elif gi.exists():
                gi.unlink()
    elif old_gi is not None or new_gi is not None:
        res.add("unchanged", ".gitignore")

    # settings.json (parsed in the preflight)
    merged, owned, notes = merge_settings(existing, render.build_settings(cfg), manifest.get("settings") or {}, force)
    text = json.dumps(merged, indent=2) + "\n"
    if not sp.exists():
        res.add("created", SETTINGS)
    elif json.loads(json.dumps(existing)) == merged:
        res.add("unchanged", SETTINGS)
    else:
        res.add("merged", SETTINGS, "your own keys are kept")
    for n in notes:
        res.lines.append("note       " + n)
    if not dry_run and (not sp.exists() or existing != merged):
        _write(sp, text)

    # manifest
    if not dry_run:
        _write(root / MANIFEST, json.dumps({"version": 1, "files": new_files, "settings": owned}, indent=2) + "\n")
    return res
