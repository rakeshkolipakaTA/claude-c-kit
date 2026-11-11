# cckit usage API

A small public HTTP app that receives Claude Code usage from developers' machines (the `cckit` hooks). One file (`usage_api.py`), no dependencies,
data in one SQLite file. Python 3.9 or newer.

## Endpoints

| Method and path | Who calls it | Auth | What it does |
|---|---|---|---|
| `POST /?api=register` | `cckit register`, or the hook at session start | the **team key** in the body | registers a company e-mail (and optionally a GitHub login) and returns a personal token for that machine |
| `POST /?api=usage` | the PreCompact hook, on every context compaction | the **token** in the body | stores one row of Claude metrics |
| `GET /?api=ping` | `cckit whoami --check`, health probes | none | `{"ok": true, "service": "cckit-usage", "ready": true}` |
| `GET /healthz` | load balancers | none | `{"ok": true}` |
| `GET /?api=export&table=events\|users&after=ID&limit=N[&format=csv]` | you, or a script | `Authorization: Bearer <admin token>` | reads the data as JSON or CSV |

Routing is by the `api` query parameter, or by the last path segment: `POST /api/register` is the same as `POST /?api=register` (likewise `/api/usage`, `/api/ping`, `/api/export`). Any prefix works, so a platform that only allows POST under `/api/` is fine. Bodies are JSON, sent as `text/plain` or `application/json`.
Application errors come back as **HTTP 200** with `{"ok": false, "error": "..."}` (so clients read one shape); transport problems use 401 (bad admin token), 403 (export not enabled),
405, 500.

### `POST /?api=register`
```
curl -s -X POST "https://usage.example.com/?api=register" -d '{"email":"ada.lovelace@tigeranalytics.com","github":"octocat","team_key":"<team key>"}'
```
```
{"ok": true, "email": "ada.lovelace@tigeranalytics.com", "github": "octocat", "token": "<64 hex characters, shown once>", "devices": 1, "verified": true}
```
- `email` (required): lower-cased, must end with an allowed domain (`CCKIT_ALLOWED_DOMAINS`, default `tigeranalytics.com`). Sub-domains and look-alikes are refused.
- `github` (optional): checked against `api.github.com` (`verified` is true only when GitHub knows the account); it never blocks registration.
- `team_key` (required): `CCKIT_TEAM_KEY`.
- Calling it again for the same e-mail registers another machine (up to 5). The 6th is refused until `reset EMAIL`.
- Errors: `email must be a @tigeranalytics.com address`, `wrong team key`, `too many registrations this hour, try later` (30 per hour, all callers), `already registered on 5 machines...`, `this user is switched off by an admin`.

### `POST /?api=usage`
```
curl -s -X POST "https://usage.example.com/?api=usage" -d '{"email":"ada.lovelace@tigeranalytics.com","token":"<token>","event_id":"6f1c...","trigger":"auto","model":"claude-sonnet-5-5","context_tokens":104220,"window":200000}'
```
```
{"ok": true}                      or      {"ok": true, "duplicate": true}      (the same event_id was already stored)
```
Required: `email`, `token`, `event_id` (unique per event, up to 64 characters). Everything else is optional; numbers are clamped to `>= 0`, text is cut to the limit and control characters
are removed, and text that starts with `=`, `+`, `-` or `@` gets a leading `'` so spreadsheets never read it as a formula.

| Field | Meaning | Limit |
|---|---|---|
| `trigger` | `auto` or `manual` | |
| `model` | model used by the newest response | 60 |
| `effort` | reasoning effort | 20 |
| `claude_version`, `entrypoint` | Claude Code version; `cli`, `claude-vscode`, ... | 20, 30 |
| `context_tokens`, `window` | context size at compaction, and the window | 5,000,000 |
| `input_tokens`, `output_tokens`, `cache_read`, `cache_write` | session totals | |
| `requests`, `sub_requests`, `sub_output`, `tool_calls` | requests (main, subagents), subagent output tokens, tool calls | |
| `models`, `tools` | `"model:requests;..."`, `"tool:count;..."` (tool **names** only) | 300 |
| `session_minutes`, `compactions_before` | session length; compactions earlier in the session | |
| `project`, `branch`, `session`, `os`, `client` | repo folder, git branch, session id, OS, client name | 80, 80, 64, 20, 40 |
| `partial` | truthy when only the tail of a huge transcript was read | |

Derived on the server: `Context %` and `Cache Hit %`. Errors: `unknown user: register first`, `bad token`, `event_id is required`, `rate limit` (120 per developer per hour), `payload too large` (over 6,000 bytes), `body must be JSON`.

### `GET /?api=export`
```
curl -s -H "Authorization: Bearer $CCKIT_ADMIN_TOKEN" "https://usage.example.com/?api=export&table=events&after=0&limit=500"
curl -s -H "Authorization: Bearer $CCKIT_ADMIN_TOKEN" "https://usage.example.com/?api=export&table=events&format=csv" > events.csv
```
JSON answer: `{"ok": true, "columns": [...], "rows": [[...]], "last_id": 123, "more": false}`. Page through events with `after=<last_id>` while `more` is true.
`table=users` returns everyone (no token hashes, a `Devices` count instead). `format=csv` returns `text/csv` with the same columns. Without `CCKIT_ADMIN_TOKEN` set on the server, export is off (403).

Event columns: Received At, Event ID, Email, GitHub, Trigger, Model, Effort, Claude Version, Entrypoint, Context Tokens, Window, Context %, Input Tokens, Output Tokens, Cache Read,
Cache Write, Cache Hit %, Requests, Subagent Requests, Subagent Output, Tool Calls, Models, Tools, Session Minutes, Compactions Before, Project, Branch, Session, OS, Partial, Client.

## Deploy

1. Make two secrets once: `python usage_api.py keys` prints `CCKIT_TEAM_KEY` (give it to developers) and `CCKIT_ADMIN_TOKEN` (for exports only). Set them as environment variables on the host.
2. Run it with a **persistent disk** (`CCKIT_DB` is one SQLite file) and **HTTPS in front** (the cckit client refuses plain http except for localhost). Any one of:

**Docker**
```
docker build -t cckit-usage .
docker run -d --name cckit-usage --restart unless-stopped -p 8080:8080 -v cckit-data:/data \
  -e CCKIT_TEAM_KEY=... -e CCKIT_ADMIN_TOKEN=... cckit-usage
```
**Plain Python** (a VM): `CCKIT_TEAM_KEY=... CCKIT_ADMIN_TOKEN=... CCKIT_DB=/var/lib/cckit/usage.db python usage_api.py run --host 127.0.0.1 --port 8080`, under systemd, with Caddy or nginx for HTTPS
(Caddyfile: `usage.example.com { reverse_proxy 127.0.0.1:8080 }`).

**gunicorn** (a PaaS that wants a WSGI app, for example Azure App Service or Render): `gunicorn -w 1 --threads 8 usage_api:application` with the same environment variables.
Keep **one worker process**; threads are fine. (SQLite handles several processes, but the hourly limits are exact only within one.)

Platforms whose containers have no persistent disk (a default Cloud Run service) lose every row on restart: attach a volume or use a VM.

3. Check it: `curl "https://usage.example.com/?api=ping"`. Then in each repo's `cckit.toml` set `endpoint = "https://usage.example.com/"` and `team_key`, run `cckit init`; `cckit whoami --check` should say `check: ok`.

## Running it
| Task | Command (same `CCKIT_DB`) |
|---|---|
| list developers | `python usage_api.py users` |
| switch off / on | `python usage_api.py deactivate EMAIL` / `activate EMAIL` |
| lost token, or a 6th machine | `python usage_api.py reset EMAIL` (they register again at the next session) |
| export everything | `python usage_api.py dump --table events --out events.csv` (or the `format=csv` endpoint) |
| back up | `sqlite3 usage.db ".backup backup.db"` |
| rotate the team key | change `CCKIT_TEAM_KEY`, restart, tell developers (existing tokens keep working) |

## What to know
- The data is employee e-mails and usage numbers (model, tokens, tool names), never prompts, code or file paths. Decide who may read the host and the database.
- Registration checks the e-mail **domain**, not ownership: anyone who has the team key can register someone else's address.
- Request bodies and query strings are never logged. The admin token only authorises reading.
- Sized for a team (hundreds of developers): one process, SQLite, a global lock.
