# claude-cost-kit

A small Python package that writes **cost-saving Claude Code configuration into a repo**, so every developer who opens
the repo gets the same cheap, safe defaults without setting anything up by hand.

```bash
pip install -e .            # once, from this folder (Python 3.11+, no dependencies)
cd your-repo
cckit init                  # creates cckit.toml and everything below; safe to re-run
cckit doctor                # checks the repo against cckit.toml
git add cckit.toml CLAUDE.md .claude .cckit && git commit
```

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
cckit register [--email E] [--github G]    register this machine for usage reports (see [telemetry])
cckit server export-app [--out DIR]        write the deployable usage app
cckit server run | keys | users | deactivate|activate|reset EMAIL | dump   run and administer the usage app
cckit whoami [--check]                     show the telemetry setting and registered user; --check pings the endpoint
cckit report [--days N] [--project TEXT] [--json]
cckit run [--profile NAME] [--effort E] [--model M] [-- claude args]
```

## Safe by design

`cckit init` never overwrites your work: keys you set in `settings.json` win (it tells you), only the marked block of `CLAUDE.md` is managed, and a generated file you edited is kept unless `--force`. A broken `settings.json` is reported, not replaced.

## Tests

```bash
pip install -e ".[dev]" && pytest
```
