# Claude Cost Kit (cckit) developer guide

cckit writes cheap, safe Claude Code settings into your repository, so everyone who opens it gets the same defaults without setting anything up. **The team lead installs it and sets up the repo once and commits the result. Everyone else runs `git pull`.**

**You need:** Git, Python 3.10+ and Claude Code, signed in.

## Team lead: set up a repo (once)

### Step 1. Install cckit (once per machine)
```bash
python3 -m venv ~/.cckit-venv
~/.cckit-venv/bin/pip install "git+https://github.com/rakeshkolipakaTA/claude-c-kit.git@main"
mkdir -p ~/.local/bin && ln -sf ~/.cckit-venv/bin/cckit ~/.local/bin/cckit
cckit --version
```
Windows PowerShell lines: [README](../README.md#quick-start-team-lead).

### Step 2. Set your company e-mail in git
```bash
git config user.email firstname.lastname@tigeranalytics.com
```
cckit takes your e-mail from here and never asks for it.

### Step 3. Preview, then set up the repo
```bash
cd your-repo
cckit init --dry-run      # shows what would change, writes nothing
cckit init
```
`init` adds:
- `.claude/settings.json`: Sonnet with high effort, limits on subagents, shorter test output, a status line (`[Model] ctx 42% $0.31`),
- `CLAUDE.md`: one short block between `cckit:begin` and `cckit:end` markers. **Your own text is never touched.**
- `.claude/agents/` (cheap read-only helpers) and `.claude/rules/` (starter coding, git and language rules),
- `.cckit/`: the settings file `cckit.toml`, the hooks and the scripts.

The first time, it asks for the **team key** (a join code from your admin). Skip it if usage reporting is off in your team. Saving the key in `.cckit/key.json` lets teammates register automatically: say yes **for a private repo only**.

### Step 4. Check it
```bash
cckit doctor              # says what to fix; every line should pass
```

### Step 5. Commit and push
```bash
git checkout -b add-cckit
git add CLAUDE.md .claude .cckit
git commit -m "Add cckit"
git push -u origin add-cckit
```
Open a pull request and merge it. Then tell the team to `git pull`.

## Everyone else
1. `git pull`.
2. Set your company e-mail in git (Step 2 above), so reporting knows who you are.
3. Open Claude Code in the repo and **accept the "trust this folder" prompt**. Without it the repo settings do not apply.

Nothing to install except Python 3 on the PATH (the hooks are small Python files). The first session registers you for usage reporting and shows one line saying so.

## Day to day
- Work as usual. At about 60% context run `/compact`, and `/clear` between unrelated tasks.
- Check your own usage: `cckit report --days 7`. It shows numbers only, read from your local sessions.
- Start Claude Code with only the tools a task needs: `cckit run --profile NAME`, with a profile your lead defined in `.cckit/cckit.toml`. `--profile none` starts with no MCP servers.
- The rule files are **starters**. Edit them to your team's standards and commit them; once you edit a file, `cckit init` never overwrites it.
- Keep `CLAUDE.md` and each rules file under 200 lines (`cckit doctor` fails above that). Put detail in `.claude/rules/` or a skill.

## Privacy
Usage reporting sends numbers and tool **names** only. It never sends prompts, code or replies. Switch it off on your machine with `CCKIT_TELEMETRY=off`. `CCKIT_DISABLED=1` makes every cckit hook do nothing.

## Best practices
- **Commit the generated files** (`CLAUDE.md`, `.claude`, `.cckit`) in a pull request, so everyone gets the same settings.
- **Keep the team key out of public repos.** `.cckit/key.json` is for private repos only; otherwise add it to `.gitignore` and give people the key.
- **Upgrade as the lead, in a pull request.** Re-install from `main`, run `cckit init`, commit the diff; everyone else `git pull`s. That keeps the team on one version.
- **A developer's own settings can override the repo's.** Only managed settings are enforced, and spend limits live in the admin console, not in the repo.
- **Review a changed rule file like code.** It steers Claude for the whole team.

## Upgrade, switch off, start over (team lead)
```bash
~/.cckit-venv/bin/pip install --upgrade "git+https://github.com/rakeshkolipakaTA/claude-c-kit.git@main"
cckit init                  # then commit the changes

cckit disable               # off for this repo, keeps your settings; cckit enable brings it back
cckit uninstall             # lists what it will remove, asks first
cckit reinstall             # uninstall, then install again
```

## If something goes wrong
| Symptom | Fix |
|---|---|
| `requires a different Python` | Delete `~/.cckit-venv` and recreate it with Python 3.10+ (`python3.12 -m venv ~/.cckit-venv`) |
| `cckit` not found | Open a new terminal, or run `~/.cckit-venv/bin/cckit` |
| pip fails with a git error | Git is missing, or github.com is not reachable |
| Settings seem ignored | Restart Claude Code and accept the folder-trust prompt |
| `cckit init` stops with a marker message | `CLAUDE.md` has a `cckit:begin` without `cckit:end` (or reversed). Fix the marker; nothing was written |
| "No company e-mail" note at start-up | `git config user.email firstname.lastname@tigeranalytics.com` |
| Not registered, nothing reported | `cckit register`, or set `CCKIT_TEAM_KEY`; check with `cckit whoami --check` |
| Windows hooks do nothing | Set `python = "python"` under `[hooks]` in `.cckit/cckit.toml`, then `cckit init` |
| Anything else | `cckit doctor`, then `cckit reinstall` |

Every command and setting: the [README](../README.md).
