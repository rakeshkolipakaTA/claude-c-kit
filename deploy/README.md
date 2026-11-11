# Hosting the cckit usage API

Use this when Google Apps Script cannot be a public web app. Developers' Claude Code hooks post to this server; your Google Sheet copies the rows
from it every 15 minutes (`Pull.gs`). The server needs a public **HTTPS** URL and a **persistent disk** (it keeps one SQLite file).

## 1. Make two secrets (once)
```
cckit server keys
```
`CCKIT_TEAM_KEY` is the join code developers use (it also goes in `cckit.toml`). `CCKIT_ADMIN_TOKEN` is only for the sheet puller. Set both as environment
variables on the host, never in a file in the repo.

## 2. Run it
Any one of these:

**Docker** (any container platform with a persistent volume):
```
docker build -f deploy/Dockerfile -t cckit-usage .
docker run -d --name cckit-usage -p 8080:8080 -v cckit-data:/data \
  -e CCKIT_TEAM_KEY=... -e CCKIT_ADMIN_TOKEN=... -e CCKIT_ALLOWED_DOMAINS=tigeranalytics.com cckit-usage
```
**A VM** (simplest, needs a domain name):
```
pip install claude-cost-kit
CCKIT_TEAM_KEY=... CCKIT_ADMIN_TOKEN=... CCKIT_DB=/var/lib/cckit/usage.db cckit server run --host 127.0.0.1 --port 8080
```
Put Caddy or nginx in front for HTTPS, for example a Caddyfile of `usage.example.com { reverse_proxy 127.0.0.1:8080 }` (Caddy gets the certificate itself).
Run the command under systemd so that it restarts.

Platforms whose containers have no persistent disk (a default Cloud Run service, for example) will lose every row on restart: attach a volume, or use a VM.

## 3. Check it
```
curl "https://usage.example.com/?api=ping"          # {"ok": true, "service": "cckit-usage", "ready": true}
```
In each repo's `cckit.toml`: `endpoint = "https://usage.example.com/"`, `team_key = "..."`, then `cckit init`, and `cckit whoami --check` should say `check: ok`.

## 4. Connect the sheet
```
cckit api export --mode puller --out apps-script
```
Paste `Pull.gs` into the Apps Script project that owns the sheet, then **Run > setupUsagePull** (or the menu item): it asks for the server URL and the admin token and installs a 15-minute trigger.
It writes the tabs `Claude_Compactions` and `Claude_Users`. No web app deployment is needed, so the "Anyone" restriction does not matter.

## Running it
| Task | Command (on the host, with the same `CCKIT_DB`) |
|---|---|
| list developers | `cckit server users` |
| switch one off / on | `cckit server deactivate EMAIL` / `cckit server activate EMAIL` |
| lost token, new laptop beyond 5 machines | `cckit server reset EMAIL` (they register again at the next session) |
| back up | copy `usage.db` (SQLite; copy while idle or use `sqlite3 usage.db ".backup backup.db"`) |
| rotate the team key | change `CCKIT_TEAM_KEY`, restart, give developers the new key (existing tokens keep working) |

## What to know
- The data is employee e-mails and usage numbers (model, tokens, tool names), never prompts or code. Treat the host and the database as you would any HR-adjacent system, and decide who may read them.
- Registration checks the e-mail **domain**, not ownership: anyone with the team key can register someone else's address.
- The server logs no request bodies or query strings. The admin token only ever authorises reading (`?api=export`).
- One process, SQLite, a global lock: this is sized for a team (hundreds of developers), not for thousands of requests per second.
