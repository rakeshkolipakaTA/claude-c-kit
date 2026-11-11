# claude-cost-kit

A small Python package that writes **cost-saving Claude Code configuration into a repo**, so every developer who opens
the repo gets the same cheap, safe defaults without setting anything up by hand.

Install with plain `pip` (Python 3.11+, no dependencies, no pipx needed). Pin the tag so everyone gets the same version:

```bash
python3 -m pip install "git+https://github.com/rakeshkolipakaTA/claude-c-kit.git@v0.2.1"
cd your-repo
cckit login                 # company e-mail + the token you were given (or `cckit init`, which asks for the e-mail and team key)
cckit doctor                # checks the repo against cckit.toml
git add cckit.toml CLAUDE.md .claude .cckit && git commit
```

- If pip says `externally-managed-environment` (recent Debian/Ubuntu/macOS Homebrew Python), use a virtual environment:
  `python3 -m venv ~/.cckit-venv && ~/.cckit-venv/bin/python -m pip install "git+https://github.com/rakeshkolipakaTA/claude-c-kit.git@v0.2.1"`,
  or add `--user`. On Windows use `py -m pip ...`.
- If `cckit` is "not found" after installing (pip's script folder is not on PATH), run it as `python3 -m cckit init` instead; it is the same program.
- From a clone of this repo: `pip install -e .`.

Edit `cckit.toml`, run `cckit init` again, and only what changed is rewritten.

## What it generates

| File | Purpose |
|---|---|
| `.claude/settings.json` | default model, effort, compaction, subagent limits, tool search, output limits, read blocks, hook, status line |
| `CLAUDE.md` | a short block of working agreements plus *Compact instructions*, between `cckit` markers (your own text stays) |
| `.claude/agents/` | `explorer` (Haiku, read-only), `test-runner` (Haiku), `reviewer` (Sonnet, read-only), each with `maxTurns` |
| `.claude/rules/testing.md` | a path-scoped rule: loads only when Claude touches tests |
| `.claude/hooks/trim_output.py` | keeps the last 100 lines of `pytest`, `npm test`, `go test` ... output (exit code preserved) |
| `.claude/hooks/statusline.py` | `[Model] ctx 42% $0.31`, with a `/compact or /clear` warning at 60% |
| `.cckit/mcp-profiles/*.json` | MCP server sets for `cckit run --profile NAME` |
| `.cckit/managed-settings.example.json` | for an **admin**: OpenTelemetry, allowed models, effort cap |

## If the repository already has a CLAUDE.md

`cckit init` never replaces it. It adds (or later updates) one block between `<!-- cckit:begin ... -->` and `<!-- cckit:end -->`; every other line, yours or added later, is left exactly as it was.
- Line endings are kept (a Windows CRLF file stays CRLF, so the diff shows only the new block).
- If the repository keeps its file at `.claude/CLAUDE.md` (and has no root `CLAUDE.md`), that one gets the block; if both exist the root file does. A `CLAUDE.md` that is a symlink to `AGENTS.md` keeps the link (the block lands in `AGENTS.md`).
- A lone or reversed marker (a begin without its end) makes `init` stop **before it writes anything**, with a message, because replacing "from the first marker to the last" could delete your text. The same goes for a `settings.json` that is not valid JSON.
- The block is short (about 20 lines) and tells Claude: be specific, plan big changes, use the cheap subagents, never paste whole logs, run `usage_report.py` for token and cost numbers (never estimate them), and never read the usage CSV. `cckit doctor` fails if a `CLAUDE.md` or rules file is over 200 lines. It never shortens your text; move detail into `.claude/rules/` or a skill.
- `cckit init --dry-run` shows what would change and writes nothing.

## Your list, and where each item went

Every setting name below was checked against the official Claude Code docs. Items marked **changed** are where the docs differ from the blog posts.

| Item | Setting | Notes |
|---|---|---|
| Default Sonnet, high reasoning | `"model": "sonnet"`, `"effortLevel": "high"` | `sonnet` is Sonnet 5.5 on the Anthropic API; other providers map it to their own version, so pin a full name in `[model] default`. High effort costs more than the Sonnet 5.5 default (medium); developers can lower it per task with `/effort`. |
| Keep CLAUDE.md under 200 lines | generated file is ~20 lines; `cckit doctor` fails above `claude_md_max_lines` | Applies to `CLAUDE.md`, nested ones and `.claude/rules/*.md`. |
| Clear between unrelated tasks, compact | working agreements + `# Compact instructions` in `CLAUDE.md` | A habit; CLAUDE.md is guidance, not enforcement. |
| `MAX_THINKING_TOKENS` 8k to 10k | **changed** | Sonnet 5+, Haiku 5.5 and Opus 4.7+ always use adaptive reasoning and ignore it. cckit only writes it when `[thinking] fixed_budget = true` (Opus/Sonnet 4.6) and warns otherwise. Use `effort` instead. |
| `CLAUDE_CODE_SUBAGENT_MODEL=haiku` | env, plus `subagent_force = true` to override agent files | A subagent file's own `model:` wins unless forced. |
| Max number of subagents | `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=4`, `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` | Claude Code defaults: 20 and 3. There is **no limit on total spawns per session**, only on concurrent ones and nesting depth. `maxTurns` caps each subagent. |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 50 | env | 1 to 100; it can only *lower* the default threshold. |
| 200k max context / auto-compact | `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` and `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` | Newer models default to a 1M window; the second variable caps them at 200k. |
| Tool names only, enable tools per task | `ENABLE_TOOL_SEARCH=true` (built in), `cckit run --profile` | With tool search, only tool *names* load up front. `cckit run --profile docs` starts Claude Code with `--strict-mcp-config` and only that profile's servers. |
| Subagent instructions and limits | `.claude/agents/*.md` | Read-only tools, a small model, `maxTurns`, and a required short report format. `Agent(model:opus)` is denied. |
| Monitoring | status line, `cckit report`, OpenTelemetry example | See below. |

Also set: `BASH_MAX_OUTPUT_LENGTH`, `MAX_MCP_OUTPUT_TOKENS`, and `Read(...)` deny rules for `node_modules`, `dist`, `build`, `.venv`, minified files.

## Monitoring

* **Live**: the status line shows context % and session cost.
* **Local, no server**: `cckit report --days 7` reads the token counts in `~/.claude/projects/**/*.jsonl` (numbers only, never message text): tokens per day and model, cache hit rate, the heaviest sessions, and a flag on sessions whose context got very long. Add `[prices.sonnet]` to `cckit.toml` to get an *estimated* cost; the real bill is in your admin console.
* **A local log of every response** (`[usage] log = true`, on by default): a `Stop` hook (and `SubagentStop`) appends one row per response to `.claude/token_usage.csv`: `timestamp, session_id, message_id, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, subagent`. Numbers only; the file stays on the machine and `cckit init` adds it to `.gitignore` (a marked block; your own lines are kept). It reads only the lines added since the last turn, never writes a message id twice (repeated events, a lost state file), waits a moment for the transcript to be flushed, also catches up on subagent transcripts, and takes a small lock file so that parallel sessions write one header and lose no offset. Claude is denied `Read` on the CSV (it grows without limit; the report script is the way in). The hook is synchronous on purpose: a background hook is killed when a headless run (`claude -p`, the SDK) exits right after the last response. Opt out with `CCKIT_USAGE_LOG=off`, or `log = false` (the hook, the CSV rule and the `.gitignore` block are removed again).
* **Scripts Claude can run** (`[usage] scripts = true`, on by default): `.claude/scripts/usage_report.py` and the `/usage` command. `python3 .claude/scripts/usage_report.py` prints this session's models, requests (main and subagent), compactions, tool calls, tokens, cache hit, context used now and estimated cost (the session is found from `CLAUDE_CODE_SESSION_ID`); `--session all --days 7 [--by day|model|session]` gives the week, `--session <id prefix>` one session, `--json` machine-readable output, `--all-projects` every project. `CLAUDE.md` tells Claude to run it instead of guessing token counts, and `cckit init` adds the narrow permission `Bash(python3 .claude/scripts/usage_report.py:*)` (it only takes effect after the developer accepts the folder-trust prompt, like every project allow rule). Costs appear only when you give your own rates in `[prices.<word in model name>]`; no prices are built in.
* **Team-wide**: put `.cckit/managed-settings.example.json` through managed settings (OpenTelemetry metrics `claude_code.token.usage` and `claude_code.cost.usage`). `OTEL_*` variables are ignored in a repo's settings file, which is why this is a separate file.

## Reporting usage to a team (optional)
`[telemetry]` in `cckit.toml` adds hooks that register each developer with a **company e-mail** (`allowed_domains`, default `tigeranalytics.com`) and report one row per context compaction
to a small app you deploy yourself (it must be reachable without a login, because a hook has none). The app is one dependency-free Python file:
`cckit server export-app --out usage-api` writes it with a Dockerfile and a README that documents every endpoint.

| Endpoint | What it does |
|---|---|
| `POST /?api=register` | registers an e-mail (+ optional GitHub login); returns a personal token for this machine |
| `POST /?api=usage` | one row per compaction: model used, effort, Claude Code version, entrypoint, context and window tokens, session input/output/cache tokens, cache hit %, requests (main and subagents), models and tool names with counts, session minutes, compactions before, repo folder and branch, OS |
| `GET /?api=ping`, `GET /healthz` | health |
| `GET /?api=export&table=events\|users[&format=csv]` | read the data (admin token) |

- **Defaults.** `[telemetry]` is on and points at the team's usage app (`register_url` / `usage_url`), but **no team key is shipped** with the package: the key is a join code from your admin. Until a machine is registered nothing is sent, and the start-up registration hook is only installed when a `team_key` is configured. `enabled = false` switches the whole feature off (and `cckit init` says so). A `endpoint` you set yourself wins over the shipped default URLs.
- **Usage and cost reports from the Stop hook** (`[telemetry] stop_report = true`, the default): after each turn the hook that writes the CSV sends the team app one event per model used in that turn (`trigger = "stop"`), so usage and cost can be read per model. Each event has the company e-mail and token and:
  - the model's tokens: input / output / cache read / cache write since the previous event (main agent **and** subagents) and its request count;
  - the turn: `turns` (how many times the Stop hook fired, i.e. prompts answered), `Duration (s)` (seconds from the prompt you typed to the last line of the answer), tool names and counts (`Tool Calls`, `Tools`; names only, never inputs or outputs);
  - the session: its id (`Session`, so distinct sessions per developer can be counted) and `Session Minutes` (minutes since the session began, a running total: use the largest value per session, never a sum);
  - the compaction setting in force on that machine: `Window` and `Compact At %`, read from `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` as Claude Code was started (so a developer who changed them is reported with their own numbers; the value in `cckit.toml` is only the fallback, and `Compact At %` is 0 when no override is set). Compaction events also carry `Context Tokens`, the size of the context when it compacted;
  - the settings in force: the current model (`Current Model`, the one that answered last), effort, Claude Code version, entrypoint, git branch, the current context size, the repo folder name and the session id.

  Turns, duration, tool counts and session minutes are put on one event of the batch (the model that wrote the most), so summing a column never counts a turn twice; the settings are on every event. No prompts, code or replies are sent. Nothing is collected until the machine is registered. If the app is unreachable or refuses (for example its hourly limit), the events wait in the state file (at most 200, retried with a growing pause) and are sent later with the same event id, so nothing is counted twice. `stop_interval = N` (seconds, default 0 = one report per turn) holds reports back so at least N seconds pass between them; the waiting numbers go out with the next one. Total cost = the sum of the `stop` events; the `auto` / `manual` events are snapshots taken at compaction and must not be added to them. Switch off with `stop_report = false` or `CCKIT_TELEMETRY=off`. The usage app has to know the `stop` trigger and the `turns`, `duration_s`, `current_model` and `compact_pct` fields (the reference `usage_api.py` does).
- **Opus reminder** (`[model] opus_reminder = true`, the default): while an Opus model is in use, a `UserPromptSubmit` hook adds a short instruction so that every reply ends with one line, `Model tip: ...`, saying whether the task needed Opus: when it is worth it (architecture and design, hard multi-file debugging, security review, large refactors, unclear or high-stakes requirements), and otherwise which cheaper model would have done and the `/model` command. On any other model the hook prints nothing and adds no tokens. A hook is not told the model, so it reads the newest main-agent response in the transcript (before the first response: `$ANTHROPIC_MODEL`, then the project settings); right after `/model` is changed the line in the managed `CLAUDE.md` block covers the gap. Off: `opus_reminder = false`, or `CCKIT_OPUS_REMINDER=off` on one machine.
- **`cckit login`** asks for your company e-mail and the token you were given (typing is hidden), saves them in `~/.config/cckit/identity.json` (owner-only, **outside any repo**, so there is nothing to git-ignore and it works for every repo on the machine), then runs `init` in the current folder. Non-interactive: `CCKIT_TOKEN=... cckit login --email NAME@tigeranalytics.com`. The token is not checked against the server at login; use `cckit whoami --check` for a reachability test.
- **`cckit init` in a terminal asks for the company e-mail** (the one in `git config user.email` is offered as the default; personal addresses are refused) and, if none is configured, the team key. It then offers to save the key in `cckit.toml` so teammates are registered automatically when they start Claude Code: say yes only for a **private** repository, because the key is committed with it. Without a terminal it never prompts; set `CCKIT_TEAM_KEY` or run `cckit register` later. A refused or skipped registration does not make `init` fail: the files are written.
- **Where the URLs live: `cckit.toml`.** Either one base `endpoint` (the APIs are `<endpoint>?api=register` and `<endpoint>?api=usage`), or each API's own URL, with `endpoint` left empty:
  ```
  [telemetry]
  enabled = true
  register_url = "https://claude-code-usage.hatchable.site/api/register"
  usage_url    = "https://claude-code-usage.hatchable.site/api/usage"
  ping_url     = ""          # optional; default: .../api/ping next to register_url (used by `cckit whoami --check`)
  team_key     = "..."
  ```
  Changing the app or the domain means editing these lines, running `cckit init` (it rewrites the hook) and committing. Each machine's token is filed under the endpoint (or the usage URL), so a new address means a new registration, which the hook does by itself at the next session.
- **Nothing to type for developers.** A `SessionStart` hook takes the company e-mail from `git config user.email` (a personal address is ignored and never sent) and, as optional extra, a GitHub login from git or `gh`; it shows one line saying what it used. With no company e-mail it asks Claude to ask the developer (at most every 3 days). Manual: `cckit register [--email E] [--github G]`, `cckit whoami --check`, `cckit doctor`.
- The personal token (one per machine, up to 5 per developer) is saved in `~/.config/cckit/identity.json` (0600, never in the repo); the app keeps only its hash. `CCKIT_TELEMETRY=off` opts one machine out; `auto_register = false` turns the automatic step off for the repo.
- The PreCompact hook runs in the background, prints nothing, reads the transcript for numbers and tool **names** only (never prompts, code, paths, tool inputs/outputs or the summary), and does nothing when unregistered or offline.
- The e-mail rule is checked in the package (before any network call) and again by the app. It checks the domain; it does not prove the person owns the mailbox.
- Run and administer the app with `cckit server run | keys | users | deactivate | activate | reset EMAIL | dump`, or the same commands on `usage_api.py`. Tests run the real app over HTTP, as WSGI, and as the exported file in its own process.

## What a repo file cannot do

* **Per-person spend limits and billing**: set in the admin console (Teams/Enterprise or Console workspaces) or in your cloud provider.
* **Force settings on developers**: a developer's own `settings.local.json` or `/model` choice can override repo settings. Only managed settings are enforced.
* **Trust**: Claude Code ignores a project's `permissions.allow` rules and most `env` values until the developer trusts the folder.

## Commands

```
cckit init [path] [--dry-run] [--force]    create/update files; --force replaces values you changed
cckit doctor [path]                        verify the repo; exit code 1 on failures
cckit print-settings [path]                show the settings.json content cckit manages
cckit login [--email E] [--token T]         save the e-mail and token you were given (home folder), then run init here
cckit register [--email E] [--github G]    register this machine for usage reports (see [telemetry])
cckit server export-app [--out DIR]        write the deployable usage app
cckit server run | keys | users | deactivate|activate|reset EMAIL | dump   run and administer the usage app
cckit whoami [--check]                     show the telemetry setting and registered user; --check pings the endpoint
cckit report [--days N] [--project TEXT] [--json]
cckit run [--profile NAME] [--effort E] [--model M] [-- claude args]
```

## Safe by design

`cckit init` never overwrites your work: keys you set in `settings.json` win (it tells you), a generated file you edited is kept unless `--force`, and a broken `settings.json` is reported, not replaced.

**If you already have a `CLAUDE.md`:** your text is never touched. cckit appends one block between `<!-- cckit:begin ... -->` and `<!-- cckit:end -->` and from then on rewrites only what is between those two lines. Anything you write before, after or between your own sections survives every re-run, and your file's line endings (Windows CRLF included) are kept, so a diff shows only the block. Details:
- A repo that keeps its memory file at `.claude/CLAUDE.md` (and has no root one) gets the block there, not a second file. If both exist, the root file is used. A `CLAUDE.md` that is a symlink to `AGENTS.md` stays a symlink, and the block goes into the target.
- If a file has a `cckit:begin` without its `cckit:end` (or the other way round, or reversed), `init` stops with a clear message and changes nothing, because guessing could delete your text.
- The block is about 20 lines. `cckit doctor` fails when `CLAUDE.md` is longer than 200 lines (every line is paid for on every turn); `init` never trims your text. The block tells Claude to run `usage_report.py` instead of guessing token counts and never to read the CSV log.

**Usage reporting and `cckit init`.** Reporting is on by default and points at the team's usage app (the two URLs in `[telemetry]`). The team key is **not** shipped in this package: `init` asks for your company e-mail (offering the one in `git config user.email`) and, if `cckit.toml` has no key, for the team key (or set `CCKIT_TEAM_KEY`). It then offers to save the key in `cckit.toml` so teammates register automatically; say yes only for a **private** repository, because the file is committed. With no terminal (CI, a script) nothing is asked: it registers silently only when a key and a company e-mail are already available, otherwise it prints what to run. A registration problem (wrong key, offline) never makes `init` fail; run `cckit register` later. To turn reporting off, set `enabled = false` in `[telemetry]`: `init` says so and asks nothing. If you set your own `endpoint`, it wins over the shipped `register_url`/`usage_url`.

## Tests

```bash
pip install -e ".[dev]" && pytest
```
