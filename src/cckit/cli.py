"""Command line: cckit init | register | whoami | server | doctor | report | run | print-settings."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import __version__, apply, config, doctor, identity, render, report, server


def _root(args) -> Path:
    return Path(args.path).resolve()


def cmd_init(args) -> int:
    try:
        res = apply.init(_root(args), force=args.force, dry_run=args.dry_run)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("\n".join(res.lines))
    print(("\nDry run: nothing was written." if args.dry_run else f"\n{res.changed} file(s) written.") +
          "\nNext: run `cckit doctor`, review the diff, and commit cckit.toml, CLAUDE.md, .claude/ and .cckit/.")
    if not args.dry_run:
        cfg = config.load(_root(args))
        t = cfg["telemetry"]
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if not t["enabled"]:
            print("\nUsage reporting to the team app is off ([telemetry] enabled = false in cckit.toml), so nobody is asked to register. Set it to true to turn it on.")
        elif not config.telemetry_active(cfg):
            print("\nUsage reporting is on but no URLs are set: give `endpoint`, or both `register_url` and `usage_url`, in [telemetry].")
        elif not identity.get_identity(config.identity_key(t)):
            print("\nUsage reporting is on for this repo, and this machine is not registered yet.")
            have_key = bool(t["team_key"] or os.environ.get("CCKIT_TEAM_KEY"))
            if interactive or (have_key and identity.detect_email(str(_root(args)), t["allowed_domains"])):
                try:
                    rc = _register_flow(cfg, _root(args), None, None, None, None)
                    if rc != 0:
                        print("Registration did not complete (see above). Run `cckit register` when you are ready.")
                    elif interactive and cfg.get("_prompted_team_key"):
                        _offer_to_save_team_key(_root(args), cfg["_prompted_team_key"], args.force)
                except (KeyboardInterrupt, EOFError):
                    print("\nSkipped. Run `cckit register` when you are ready.")
            else:
                print("Run `cckit register` (it asks for your company e-mail and the team key), or set CCKIT_TEAM_KEY and start Claude Code here: "
                      "it registers you from the company e-mail in your git config.")
    return 0


def _save_team_key(root: Path, key: str) -> bool:
    """Write team_key into cckit.toml's [telemetry] section (replacing the value, or adding the line). False if the key looks unsafe to write."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{4,128}", key):
        return False
    p = root / config.CONFIG_NAME
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if re.search(r'(?m)^team_key\s*=\s*"[^"]*"', text):
        text = re.sub(r'(?m)^(team_key\s*=\s*)"[^"]*"', lambda m: f'{m.group(1)}"{key}"', text, count=1)
    elif re.search(r"(?m)^\[telemetry\]", text):
        text = re.sub(r"(?m)^(\[telemetry\][^\n]*\n)", lambda m: f'{m.group(1)}team_key = "{key}"\n', text, count=1)
    else:
        text += ("\n" if text and not text.endswith("\n") else "") + f'\n[telemetry]\nteam_key = "{key}"\n'
    p.write_text(text, encoding="utf-8", newline="\n")
    return True


def _offer_to_save_team_key(root: Path, key: str, force: bool) -> None:
    print("\nSave the team key in cckit.toml so teammates are registered automatically when they start Claude Code here?\n"
          "It is committed with the repository, so say yes only if the repository is private.")
    if input("Save the team key? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("Not saved. Teammates run `cckit register` (and enter the key) or set CCKIT_TEAM_KEY.")
        return
    if not _save_team_key(root, key):
        print("The key has characters cckit will not write into a file; edit cckit.toml by hand.")
        return
    apply.init(root, force=force)                       # regenerate the hooks: they now carry the key
    print("Saved to cckit.toml; the hooks were regenerated. Commit cckit.toml and .claude/.")


def _register_flow(cfg: dict, root: Path, email: str | None, github: str | None, endpoint: str | None, team_key: str | None) -> int:
    t = cfg["telemetry"]
    urls = {"endpoint": endpoint} if endpoint else t                 # --endpoint overrides every URL in cckit.toml
    key, register_url = config.identity_key(urls), config.api_url(urls, "register")
    if not register_url or not config.valid_endpoint(register_url):
        print("error: no valid register URL. Set [telemetry] endpoint (or register_url) in cckit.toml to an https:// URL, or pass --endpoint.", file=sys.stderr)
        return 2
    domains = t["allowed_domains"]
    interactive = sys.stdin.isatty()
    if not email:
        found = identity.detect_email(str(root), domains)
        if found and not interactive:
            email = found[0]
            print(f"Using {email} (from {found[1]}).")
        elif interactive:
            for _ in range(3):
                ans = input(f"Your company e-mail ({' / '.join('@' + d for d in domains)})" + (f" [{found[0]}, from {found[1]}]" if found else "") + ": ").strip()
                email = ans or (found[0] if found else "")
                problem = identity.email_problem(email, domains)
                if not problem:
                    break
                print(problem)
            else:
                return 2
        else:
            print("error: no company e-mail found in git config; pass --email NAME@" + domains[0], file=sys.stderr)
            return 2
    problem = identity.email_problem(email, domains)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    if not github:
        found_gh = identity.detect_github(str(root))
        github = found_gh[0] if found_gh else None            # optional extra: never asked for
    team_key = team_key or os.environ.get("CCKIT_TEAM_KEY") or t["team_key"]
    if not team_key:
        if not interactive:
            print("error: no team key. Set [telemetry] team_key, CCKIT_TEAM_KEY, or pass --team-key.", file=sys.stderr)
            return 2
        team_key = input("Team key (from your admin): ").strip()
        cfg["_prompted_team_key"] = team_key
    try:
        res = identity.register(key, register_url, team_key, email, github, domains)
    except identity.RegisterError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Registered {res.get('email', email)}" + (f" (GitHub: {res['github']})" if res.get("github") else "") +
          f". Token saved to {identity.identity_path()} (keep it private, never commit it).")
    return 0


def cmd_register(args) -> int:
    try:
        cfg = config.load(_root(args))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        return _register_flow(cfg, _root(args), args.email, args.github, args.endpoint, args.team_key)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130


def cmd_server(args) -> int:
    """`cckit server ...` is the standalone usage app's own command line (see server.py / usage_api.py)."""
    rest = list(args.server_args)
    if rest and rest[0] == "export-app":
        return _export_app(rest[1:])
    return server.main(rest)


def _export_app(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="cckit server export-app", description="write the deployable usage API app (usage_api.py, Dockerfile, README...)")
    ap.add_argument("--out", default="usage-api", help="folder to write (default ./usage-api)")
    ap.add_argument("--check", action="store_true", help="only compare the folder with this version of cckit; exit 1 when it differs")
    a = ap.parse_args(argv)
    files = render.build_usage_app()
    out = Path(a.out)
    if a.check:
        bad = [n for n, t in files.items() if not (out / n).exists() or (out / n).read_text(encoding="utf-8") != t]
        for n in bad:
            print(f"differs: {out / n}")
        print("up to date" if not bad else f"{len(bad)} file(s) differ from this version of cckit. Run `cckit server export-app --out {out}`.")
        return 1 if bad else 0
    for n, t in files.items():
        (out / n).parent.mkdir(parents=True, exist_ok=True)
        (out / n).write_text(t, encoding="utf-8", newline="\n")
        print(f"wrote {out / n}")
    print(f"\nDeploy {out}/ on its own (see {out}/README.md). It does not need cckit: usage_api.py is one file with no dependencies.")
    return 0


def cmd_whoami(args) -> int:
    try:
        cfg = config.load(_root(args))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    t = cfg["telemetry"]
    print(f"telemetry:  {'on' if t['enabled'] else 'off'} (events: {', '.join(t['events'])})")
    key = config.identity_key(t)
    show = lambda u: u or "(not set)"
    print(f"endpoint:   {show(t['endpoint'])}")
    who = identity.get_identity(key) if key else None
    print(f"registered: {(who['email'] + (' / GitHub ' + who['github'] if who.get('github') else '')) if who else 'no'}   (identity file: {identity.identity_path()})")
    print(f"register:   {show(config.api_url(t, 'register'))}")
    print(f"usage:      {show(config.api_url(t, 'usage'))}")
    print(f"ping:       {show(config.api_url(t, 'ping'))}")
    if args.check:
        ping_url = config.api_url(t, "ping")
        if not ping_url:
            print("check:      skipped, no URL (set endpoint, or ping_url)")
            return 1
        try:
            d = identity.ping(ping_url)
        except identity.RegisterError as e:
            print(f"check:      FAILED, {e}")
            return 1
        print(f"check:      {'ok' if d.get('ok') else 'FAILED'}" + ("" if d.get("ready", True) else " (the app answered but says it is not ready: its team key or admin token is not configured)"))
        return 0 if d.get("ok") and d.get("ready", True) else 1
    return 0


def cmd_doctor(args) -> int:
    rep = doctor.run(_root(args), check_claude=not args.no_claude)
    print(rep.render())
    return 1 if rep.failed else 0


def cmd_print(args) -> int:
    try:
        cfg = config.load(_root(args))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    errs = config.validate(cfg)
    if errs:
        print("\n".join(f"error: {e}" for e in errs), file=sys.stderr)
        return 2
    print(json.dumps(render.build_settings(cfg), indent=2))
    return 0


def cmd_report(args) -> int:
    base = Path(args.dir).expanduser() if args.dir else report.default_dir()
    if not base.exists():
        print(f"No transcripts found at {base}", file=sys.stderr)
        return 1
    prices = {}
    try:
        prices = config.load(_root(args)).get("prices", {})
    except ValueError:
        pass
    summary = report.summarise(report.collect(base, days=args.days, project=args.project))
    if args.json:
        out = {"by_day_model": {f"{d}|{m}": t.__dict__ for (d, m), t in summary["by_day_model"].items()},
               "sessions": {sid: {"project": s.project, "first": s.first, "requests": s.tot.requests, "max_context": s.max_context,
                                  "output": s.tot.output + s.side.output, "subagent_requests": s.side.requests}
                            for sid, s in summary["sessions"].items()}}
        print(json.dumps(out, indent=2))
    else:
        print(report.render(summary, prices=prices, top=args.top))
    return 0


def build_run_command(root: Path, profile: str | None, model: str | None, effort: str | None, extra: list[str]) -> list[str]:
    cmd = ["claude"]
    if profile:
        path = root / ".cckit" / "mcp-profiles" / f"{profile}.json"
        if not path.exists():
            names = sorted(p.stem for p in (root / ".cckit" / "mcp-profiles").glob("*.json")) if (root / ".cckit" / "mcp-profiles").exists() else []
            raise ValueError(f"no MCP profile '{profile}'. Available: {', '.join(names) or 'none (run `cckit init`)'}")
        cmd += ["--strict-mcp-config", "--mcp-config", str(path)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    return cmd + extra


def cmd_run(args) -> int:
    extra = list(args.claude_args)
    if extra and extra[0] == "--":
        extra = extra[1:]
    try:
        cmd = build_run_command(_root(args), args.profile, args.model, args.effort, extra)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(" ".join(cmd))
        return 0
    exe = shutil.which("claude")
    if not exe:
        print("error: `claude` is not on PATH", file=sys.stderr)
        return 127
    os.chdir(_root(args))
    os.execv(exe, [exe] + cmd[1:])
    return 0  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cckit", description="Cost-saving Claude Code configuration, generated into your repo.")
    ap.add_argument("--version", action="version", version=f"cckit {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("path", nargs="?", default=".", help="repo root (default: current directory)")

    p = sub.add_parser("init", help="create or update cckit.toml, settings, CLAUDE.md, subagents, hooks")
    common(p)
    p.add_argument("--force", action="store_true", help="overwrite files and values you changed")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="check the repo against cckit.toml")
    common(p)
    p.add_argument("--no-claude", action="store_true", help="do not call `claude --version`")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("print-settings", help="print the settings.json content cckit would manage")
    common(p)
    p.set_defaults(fn=cmd_print)

    p = sub.add_parser("report", help="token usage from local Claude Code sessions")
    common(p)
    p.add_argument("--dir", help="transcripts folder (default ~/.claude/projects)")
    p.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    p.add_argument("--project", help="only projects whose folder name contains this text")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("register", help="register this machine's GitHub user name for compaction reports")
    common(p)
    p.add_argument("--email", help="your company e-mail (default: `git config user.email` when it is on an allowed domain, else asked)")
    p.add_argument("--github", help="your GitHub login, optional (default: found from git or gh when possible)")
    p.add_argument("--endpoint", help="usage endpoint (default: [telemetry] endpoint)")
    p.add_argument("--team-key", help="team join code (default: [telemetry] team_key or $CCKIT_TEAM_KEY)")
    p.set_defaults(fn=cmd_register)

    p = sub.add_parser("server", help="the standalone usage API app: run | keys | users | deactivate | activate | reset | dump | export-app")
    p.add_argument("server_args", nargs=argparse.REMAINDER, help="run with --help after the word server's subcommand, e.g. `cckit server run --help`")
    p.set_defaults(fn=cmd_server)

    p = sub.add_parser("whoami", help="show the telemetry setting and who this machine is registered as")
    common(p)
    p.add_argument("--check", action="store_true", help="also ping the endpoint")
    p.set_defaults(fn=cmd_whoami)

    p = sub.add_parser("run", help="start Claude Code with only the MCP servers a task needs")
    p.add_argument("--path", default=".", help="repo root (default: current directory)")
    p.add_argument("--profile", help="MCP profile from .cckit/mcp-profiles (use `none` for no MCP servers)")
    p.add_argument("--model")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--dry-run", action="store_true", help="print the command, do not start Claude Code")
    p.add_argument("claude_args", nargs=argparse.REMAINDER, help="arguments passed to claude (put them after --)")
    p.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
