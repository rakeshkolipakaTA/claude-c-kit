"""`cckit uninstall`: take cckit out of a repo again, touching only what cckit put there.

  * generated files (hooks, scripts, agents, rules, MCP profiles, examples): removed while nobody has edited them (the hash in .cckit/manifest.json says
    what cckit last wrote); an edited file is kept and listed, unless --force;
  * CLAUDE.md and .gitignore: only the text between the cckit markers goes; the rest is yours, line endings included. A file that held nothing else is removed;
  * .claude/settings.json: the values, permission rules and hooks cckit added are taken back (the same bookkeeping `init` uses); your own keys stay;
  * your settings and data stay unless --purge: .cckit/cckit.toml, .cckit/key.json and the local usage log. Without --purge a later `cckit init` restores the same setup.
This machine's registration (~/.config/cckit/identity.json) is never touched: re-registering would use up one of the five device slots per e-mail.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import apply, config, render


def _rmdirs(p: Path, root: Path) -> None:
    """Remove the folders a deleted file leaves empty, up to (not including) the repo root."""
    d = p.parent
    while d != root and d.is_dir():
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent


def _owned_settings_fallback(cfg: dict) -> dict:
    """With no manifest, treat what the current config would write as cckit's own (an exact match is the only thing taken back)."""
    want = render.build_settings(cfg)
    return {"env": want.get("env") or {}, "deny": (want.get("permissions") or {}).get("deny") or [],
            "allow": (want.get("permissions") or {}).get("allow") or [],
            "scalars": {k: want[k] for k in ("model", "effortLevel", "statusLine") if k in want}, "hooks": list((want.get("hooks") or {}).keys())}


def uninstall(root: Path, purge: bool = False, force: bool = False, dry_run: bool = False) -> apply.Result:
    root = Path(root)
    res = apply.Result()
    manifest = apply.load_manifest(root)
    try:
        cfg = config.load(root)
    except ValueError:
        cfg = config.defaults()                                  # an unreadable config must not stop the removal

    # Preflight: what can make this refuse is checked before anything is touched.
    cm = apply.claude_md_path(root)
    raw_md = cm.read_bytes().decode("utf-8") if cm.exists() else None
    old_md = raw_md.replace("\r\n", "\n") if raw_md is not None else None
    crlf = raw_md is not None and "\r\n" in raw_md
    new_md = render.remove_block(old_md) if old_md is not None else None
    sp = root / apply.SETTINGS
    existing: dict = {}
    if sp.exists():
        try:
            existing = json.loads(sp.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"{apply.SETTINGS} is not valid JSON ({e}). Fix it first; nothing was changed.") from e
        if not isinstance(existing, dict):
            raise ValueError(f"{apply.SETTINGS} must contain a JSON object; nothing was changed.")

    # 1. generated files
    owned = dict(manifest.get("files") or {})
    if not owned:                                               # no manifest: only a file that is exactly what this version would write is taken
        owned = {rel: apply.sha(text) for rel, text in apply.desired_files(cfg).items()}
    for rel, h in owned.items():
        p = root / rel
        if not p.exists():
            continue
        try:
            same = apply.sha(p.read_text(encoding="utf-8")) == h
        except (OSError, UnicodeDecodeError):
            same = False
        if same or force:
            res.add("removed", rel, "" if same else "you edited it; removed because of --force")
            if not dry_run:
                p.unlink()
                _rmdirs(p, root)
        else:
            res.add("kept", rel, "you edited it; use --force to remove it too")

    # 2. CLAUDE.md
    rel_cm = str(cm.relative_to(root)).replace("\\", "/")
    if old_md is not None and new_md != old_md:
        res.add("removed" if new_md is None else "merged", rel_cm, "the file only held the cckit block" if new_md is None else "the managed block only; your text is kept")
        if not dry_run:
            if new_md is None:
                cm.unlink()
            else:
                cm.write_bytes((new_md.replace("\n", "\r\n") if crlf else new_md).encode("utf-8"))

    # 3. .gitignore
    gi = root / ".gitignore"
    if gi.exists():
        old_gi = gi.read_text(encoding="utf-8")
        new_gi = render.upsert_gitignore(old_gi, None)
        if new_gi != old_gi:
            res.add("removed" if not new_gi else "merged", ".gitignore", "the managed block only" if new_gi else "the file only held the cckit block")
            if not dry_run:
                if new_gi:
                    apply._write(gi, new_gi)
                else:
                    gi.unlink()

    # 4. settings.json: take back what cckit added
    if sp.exists():
        prev = manifest.get("settings") or _owned_settings_fallback(cfg)
        if "hooks" not in prev or not prev["hooks"]:
            prev = dict(prev, hooks=list((render.build_settings(cfg).get("hooks") or {}).keys()))
        merged, _, _ = apply.merge_settings(existing, {"env": {}, "permissions": {"deny": []}}, prev, force)
        if merged != existing:
            if not merged:
                res.add("removed", apply.SETTINGS, "it held only what cckit added")
                if not dry_run:
                    sp.unlink()
                    _rmdirs(sp, root)
            else:
                res.add("merged", apply.SETTINGS, "what cckit added is taken back; your own keys are kept")
                if not dry_run:
                    apply._write(sp, json.dumps(merged, indent=2) + "\n")
        else:
            res.add("unchanged", apply.SETTINGS, "nothing of cckit's in it")

    # 5. the manifest, and with --purge also the settings and data
    man = root / apply.MANIFEST
    if man.exists():
        res.add("removed", apply.MANIFEST)
        if not dry_run:
            man.unlink()
    csv = root / cfg["usage"]["csv_path"]
    data = [(config.CONFIG_NAME, root / config.CONFIG_NAME), (config.LEGACY_CONFIG_NAME, root / config.LEGACY_CONFIG_NAME),
            (config.KEY_NAME, root / config.KEY_NAME)] + [(cfg["usage"]["csv_path"] + suf, Path(str(csv) + suf)) for suf in ("", ".state.json", ".lock")]
    for rel, p in data:
        if not p.exists():
            continue
        if purge:
            res.add("removed", rel)
            if not dry_run:
                p.unlink()
                _rmdirs(p, root)
        else:
            res.add("kept", rel, "yours: a later `cckit init` uses it again; --purge removes it")
    if not dry_run:
        for d in (root / ".cckit",):
            try:
                d.rmdir()
            except OSError:
                pass
    return res
