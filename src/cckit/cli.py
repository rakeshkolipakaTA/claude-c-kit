"""Command line: cckit init | register | whoami | api export | server | doctor | report | run | print-settings."""
from __future__ import annotations

import argparse
import json
import os
import secrets
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
        if config.telemetry_active(cfg) and not identity.get_identity(cfg["telemetry"]["endpoint"]):
            print("\nCompaction reports are on for this repo and this machine is not registered yet.")
            have_key = bool(cfg["telemetry"]["team_key"] or os.environ.get("CCKIT_TEAM_KEY"))
            if sys.stdin.isatty() and sys.stdout.isatty() or (have_key and identity.detect_email(str(_root(args)), cfg["telemetry"]["allowed_domains"])):
                try:
                    return _register_flow(cfg, _root(args), None, None, None, None)
                except (KeyboardInterrupt, EOFError):
                    print("\nSkipped. Run `cckit register` when you are ready.")
            else:
                print("Run `cckit register`, or just start Claude Code here: it registers you from the company e-mail in your git config.")
    return 0


def _register_flow(cfg: dict, root: Path, email: str | None, github: str | None, endpoint: str | None, team_key: str | None) -> int:
    t = cfg["telemetry"]
    endpoint = endpoint or t["endpoint"]
    if not endpoint or not config.valid_endpoint(endpoint):
        print("error: no valid endpoint. Set [telemetry] endpoint in cckit.toml (an https:// URL) or pass --endpoint.", file=sys.stderr)
        return 2
    urls = {"endpoint": endpoint} if endpoint != t["endpoint"] else t
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
    try:
        res = identity.register(endpoint, config.api_url(urls, "register"), team_key, email, github, domains)
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


def cmd_api(args) -> int:
    if args.api_cmd != "export":
        return 2
    files = render.build_apps_script(standalone=args.standalone, mode=args.mode)
    out = Path(args.out)
    if args.check:
        bad = [n for n, text in files.items() if not (out / n).exists() or (out / n).read_text(encoding="utf-8") != text]
        for n in bad:
            print(f"differs: {out / n}")
        print("up to date" if not bad else f"{len(bad)} file(s) differ from this version of cckit. Run `cckit api export --out {out}`.")
        return 1 if bad else 0
    out.mkdir(parents=True, exist_ok=True)
    for n, text in files.items():
        (out / n).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {out / n}")
    if args.mode == "puller":
        print("\nPaste Pull.gs into the Apps Script project that owns the sheet, run setupUsagePull once (it asks for the server URL and admin token\n"
              "and installs a 15-minute trigger). No web app deployment is needed.")
    else:
        print("\nPaste these into the Apps Script project that owns the sheet (or clasp push), run setupUsageApi once,\n"
              "then Deploy > New deployment > Web app: Execute as Me, access Anyone. Put the /exec URL in cckit.toml [telemetry] endpoint.")
    return 0


def _usage_api(args, need_key: bool = True) -> "server.UsageApi":
    env = os.environ
    domains = (args.domains or env.get("CCKIT_ALLOWED_DOMAINS") or "tigeranalytics.com").split(",")
    key = env.get("CCKIT_TEAM_KEY", "")
    if need_key and not key:
        raise ValueError("set CCKIT_TEAM_KEY (the join code developers use; `cckit server keys` makes one)")
    return server.UsageApi(args.db or env.get("CCKIT_DB") or "usage.db", key or "-", domains, env.get("CCKIT_ADMIN_TOKEN", ""))


def cmd_server(args) -> int:
    c = args.server_cmd
    if c == "keys":
        print(f"CCKIT_TEAM_KEY={secrets.token_hex(8)}\nCCKIT_ADMIN_TOKEN={secrets.token_hex(24)}")
        print("\nSet both as environment variables on the host (never in a file in the repo). Developers get the team key; the admin token is for the sheet puller only.", file=sys.stderr)
        return 0
    try:
        api = _usage_api(args, need_key=(c == "run"))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if c == "run":
        host = args.host or os.environ.get("CCKIT_HOST") or "127.0.0.1"
        port = args.port or int(os.environ.get("PORT") or 8080)
        srv = server.make_server(host, port, api)
        print(f"cckit usage API listening on http://{host}:{port}  (db: {api.db_path}; domains: {', '.join(api.domains)})")
        print("export for the sheet: " + ("enabled" if api.admin_token else "DISABLED (set CCKIT_ADMIN_TOKEN)") + ". Put HTTPS in front of this port: clients refuse plain http.", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        return 0
    if c == "users":
        rows = api.export("users")["rows"]
        print(f"{'email':<42} {'github':<20} {'devices':>7} {'active':<6} {'compactions':>11}  last seen")
        for r in rows:
            print(f"{r[0]:<42} {r[1]:<20} {r[4]:>7} {r[5]:<6} {r[7]:>11}  {r[6]}")
        print(f"\n{len(rows)} user(s)")
        return 0
    done = {"deactivate": lambda: api.set_active(args.email, False), "activate": lambda: api.set_active(args.email, True), "reset": lambda: api.reset(args.email)}[c]()
    print(f"{c}: {args.email}" if done else f"no such user: {args.email}")
    return 0 if done else 1


def cmd_whoami(args) -> int:
    try:
        cfg = config.load(_root(args))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    t = cfg["telemetry"]
    print(f"telemetry:  {'on' if t['enabled'] else 'off'} (events: {', '.join(t['events'])})")
    print(f"endpoint:   {t['endpoint'] or '(not set)'}")
    who = identity.get_identity(t["endpoint"]) if t["endpoint"] else None
    print(f"registered: {(who['email'] + (' / GitHub ' + who['github'] if who.get('github') else '')) if who else 'no'}   (identity file: {identity.identity_path()})")
    print(f"register:   {config.api_url(t, 'register') if t['endpoint'] else '(not set)'}")
    print(f"usage:      {config.api_url(t, 'usage') if t['endpoint'] else '(not set)'}")
    if args.check:
        if not t["endpoint"]:
            print("check:      skipped, no endpoint")
            return 1
        try:
            d = identity.ping(t["endpoint"])
        except identity.RegisterError as e:
            print(f"check:      FAILED, {e}")
            return 1
        print(f"check:      {'ok' if d.get('ok') else 'FAILED'}" + ("" if d.get("ready", True) else " (endpoint reachable but not set up: admin must run 'Set up Claude usage API')"))
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

    p = sub.add_parser("api", help="the server side: export the register and usage APIs as Google Apps Script files")
    asub = p.add_subparsers(dest="api_cmd", required=True)
    pe = asub.add_parser("export", help="write Api.gs, Register.gs and Usage.gs")
    pe.add_argument("--out", default=".cckit/apps-script", help="folder to write (default .cckit/apps-script)")
    pe.add_argument("--mode", choices=["direct", "puller"], default="direct",
                    help="direct: Apps Script hosts both APIs (needs a deployment with access Anyone). puller: only Pull.gs, which copies rows from a `cckit server` into the sheet")
    pe.add_argument("--standalone", action="store_true", help="also write Standalone.gs (doGet and menu) for a project that is not the Access Hub")
    pe.add_argument("--check", action="store_true", help="only compare the folder with this version of cckit; exit 1 when it differs")
    p.set_defaults(fn=cmd_api)

    p = sub.add_parser("server", help="run the register and usage APIs as a standalone public service (when Apps Script cannot be public)")
    ssub = p.add_subparsers(dest="server_cmd", required=True)
    pr = ssub.add_parser("run", help="start the server (config by environment: CCKIT_TEAM_KEY, CCKIT_ADMIN_TOKEN, CCKIT_ALLOWED_DOMAINS, CCKIT_DB, PORT)")
    pr.add_argument("--host", help="default 127.0.0.1 (use 0.0.0.0 in a container)")
    pr.add_argument("--port", type=int, help="default $PORT or 8080")
    ssub.add_parser("keys", help="print a fresh team key and admin token")
    for name, h in (("users", "list registered developers"), ("deactivate", "switch a developer off"), ("activate", "switch a developer on"),
                    ("reset", "forget a developer's machine tokens (they register again at the next session)")):
        sp = ssub.add_parser(name, help=h)
        if name != "users":
            sp.add_argument("email")
    for sp in ssub.choices.values():
        if sp.prog.endswith(" keys"):
            continue
        sp.add_argument("--db", help="SQLite file (default $CCKIT_DB or usage.db)")
        sp.add_argument("--domains", help="allowed e-mail domains, comma separated (default $CCKIT_ALLOWED_DOMAINS or tigeranalytics.com)")
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
