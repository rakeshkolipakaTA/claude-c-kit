# Dashboard: separate billed usage from seat usage

Since cckit 0.2.7 every usage event says **how the developer is billed**. Use it to keep two different questions apart:

- *What are we paying?* Only **metered** usage (API key, Bedrock, Vertex, Foundry) has a cost per token.
- *How are we using Claude?* **Subscription** usage (a Claude.ai Pro, Max, Team or Enterprise seat) costs nothing per token. A dollar figure for it matches no bill, so show tokens, requests and time instead.

## 1. What changes in the API

Events (`POST /api/usage`, both `trigger = "stop"` and the compaction snapshots `auto` / `manual`) have two new fields:

| Field | Values | Meaning |
|---|---|---|
| `billing` | `subscription`, `metered`, `unknown` | how this developer's Claude Code is billed |
| `plan` | `pro`, `max`, `team`, `enterprise`, `api`, `bedrock`, `vertex`, `foundry`, or empty | the plan or provider behind it |
| `org_name` | text, for example `Claude-Cluster3` | the organisation of the Claude login (empty for API key / provider) |
| `org_type` | for example `claude_team` | the organisation type |
| `seat_tier` | for example `team_standard` | the seat tier within the plan |
| `billing_type` | for example `stripe_subscription` | how Claude bills that account |
| `extra_usage` | `yes`, `no`, empty | whether pay-per-token usage beyond the seat is switched on |
| `rate_limit_tier` | for example `default_raven` | Claude's internal rate-limit tier (informational) |

The last six come from the developer's Claude login only (never the e-mail, name, ids or tokens) and are empty for `metered` events. New values can appear at any time (new plans, tiers): store them as free text, never as a fixed list, and never reject an event for an unknown value.

Server rules:

1. Add nullable text columns to `events`: `billing`, `plan`, `org_name`, `org_type`, `seat_tier`, `billing_type`, `extra_usage`, `rate_limit_tier`.
2. Store `billing` only if it is `subscription` or `metered`; anything else (missing, empty, unknown text) becomes `unknown`. Lowercase `plan` and cut it to 20 characters; cut the other texts to 60.
3. Old clients (before 0.2.7) send neither field: accept the event and store `unknown`. Never reject on a missing field.
4. Classify **per event, not per user**. One person can have both (a repo can force `billing`, or they may use an API key on one project and a seat on another).
5. **Cost is computed only from `trigger = "stop"` rows with `billing = "metered"`.** Never price `subscription` or `unknown` rows, and never add the `auto` / `manual` snapshots to a total (this rule was already true; it matters more now).
6. Old rows have no value. Show them as `unknown`. Optional backfill: for a user whose newer events are all one billing type, mark their older rows with that type and flag them "assumed".

The reference `usage_api.py` (`cckit server export-app`) already does 1-3 and exports `Billing` and `Plan` columns.

## 2. Layout

Three tabs under one shared filter bar. Besides date, developer, project and model, the filters are **Organisation**, **Org type**, **Plan**, **Seat tier** and **Extra usage** (yes / no), so cost and usage can be cut by who is on which plan. Add `plan` / `seat tier` / `org` as "group by" options on every table and chart. The tab names carry the counts, so a tab that needs attention is visible without opening it.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Claude Code usage                          [ Last 30 days ▾ ] [ All users ▾ ]│
│                                            [ All projects ▾ ] [ All models ▾ ]│
├──────────────────────────────────────────────────────────────────────────────┤
│  Billed (metered) 3        Seat (subscription) 14        Unclassified 2      │
│  ═════════════════                                                            │
```

### Tab 1: Billed (metered): money

```
│  Estimated cost      Tokens (in / out / cached)    Requests     Avg $/turn   │
│  $412.80             38M / 6.1M / 210M             9,420        $0.044       │
│  ▲ 12% vs prior 30d                                                          │
│                                                                              │
│  Cost per day (stacked by model)                                             │
│  $30 ┤        ▂▅                                                             │
│  $15 ┤  ▂▃▅▇▆▅██▆▃▂▂▅▇                  ■ Sonnet  ■ Opus  ■ Haiku           │
│   $0 └────────────────────────────────                                       │
│                                                                              │
│  Cost by developer          Cost by model          Cost by project           │
│  ada.l…     $210.10  51%    Sonnet 5.5  $301  73%   pricing-portal  $260     │
│  grace.h…   $140.40  34%    Opus 5.5    $98   24%   etl             $101     │
│  linus.t…   $62.30   15%    Haiku 5.5   $13    3%   infra            $52     │
│                                                                              │
│  ⚠ 214 events not priced: model "claude-x" is not in the price table [Fix]   │
```

- Dollar amounts here and nowhere else. Label it "Estimated cost" and keep the existing "pricing" admin page.
- Keep the existing "unpriced events" warning, scoped to this tab.
- Show the plan chip next to each developer (`api`, `bedrock`...).

### Tab 2: Seat (subscription): usage, no dollars

```
│  Active developers   Turns        Active time      Tokens (in / out / cached)│
│  14                  18,230       212 h            71M / 11M / 640M          │
│                                                                              │
│  Tokens per day                         Model mix (by output tokens)         │
│  ▂▃▅▇▆▅██▆▃▂▂▅▇                          Sonnet ██████████████ 78%           │
│                                          Opus   ████ 19%                     │
│                                          Haiku  █ 3%                         │
│                                                                              │
│  Developer     Plan  Sessions  Turns  Active h  Avg session  Tokens  Opus %  │
│  ada.l…        team  41        1,204  38.5      21 min       9.8M    12%     │
│  grace.h…      max   28          870  30.1      27 min       7.1M    31%     │
│                                                                              │
│  Included in each developer's seat; not billed per token.                    │
│  [ ] Show API-equivalent value   (off by default; "not a bill")              │
```

- **No dollar amounts by default.** The optional toggle shows "API-equivalent value" in a muted style with the words "not billed" next to it, for people who want a feel for the scale.
- The useful questions here are adoption and habits: who uses it, how long, how much Opus (the part that hits seat limits first), and compaction counts.
- **Extra usage = yes** marks developers whose seat can bill pay-per-token usage beyond the limit. Show a small amber "extra usage on" chip next to them and a tile "Developers with extra usage on: N". Their overage is **not** in these events (Claude Code does not report it); it appears on the Anthropic invoice.

### Tab 3: Unclassified

```
│  2 developers report no billing type: they run an older cckit (before 0.2.7) │
│  or Claude Code's login could not be read.                                   │
│                                                                              │
│  Developer       Last seen     cckit client      Events    Tokens            │
│  linus.t…        2 days ago    cckit-stop/2      312       4.1M              │
│                                                                              │
│  Fix: pull the latest repo changes (the lead upgrades cckit and runs         │
│  `cckit init`). Until then their tokens are counted but not priced.          │
```

- Show the tab only when it has rows, or show it dimmed with a 0 count.
- Tokens still count in "All usage" totals so nothing disappears.

## 3. Rules that keep the numbers honest

- **Reconcile.** total tokens = metered + subscription + unknown. Show that line in a small footer.
- **Turns, duration and tool counts are on one event per batch** (the model that wrote the most): summing a column is right.
- **Session minutes are a running total**: take the largest value per session, never a sum. Active time = sum of `duration_s` (time spent answering), not session minutes.
- **Developers, sessions and distinct models** are counted over the filtered events, per tab.
- **Do not guess a plan from the e-mail.** Only the `plan` and `seat_tier` fields say it. A developer's plan can change over time: filter by the value on each event, not by the latest value for the user.
- Hide a developer's e-mail beyond the local part for people who are not admins, if the dashboard has viewer roles.

## 4. SQL (SQLite) for the main numbers

```sql
-- cost: metered stop events only; join your price table on model
SELECT e.model, SUM(e.input_tokens) AS input, SUM(e.output_tokens) AS output,
       SUM(e.cache_read) AS cache_read, SUM(e.cache_write) AS cache_write
FROM events e
WHERE e.trig = 'stop' AND e.billing = 'metered'
GROUP BY e.model;

-- seat usage per developer
SELECT email, plan, COUNT(DISTINCT session) AS sessions, SUM(turns) AS turns,
       ROUND(SUM(COALESCE(duration_s, 0)) / 3600.0, 1) AS active_hours,
       SUM(input_tokens + output_tokens + cache_read + cache_write) AS tokens,
       ROUND(100.0 * SUM(CASE WHEN model LIKE '%opus%' THEN output_tokens ELSE 0 END) / NULLIF(SUM(output_tokens), 0)) AS opus_pct
FROM events
WHERE trig = 'stop' AND billing = 'subscription'
GROUP BY email, plan;

-- who is unclassified
SELECT email, MAX(received_at) AS last_seen, MAX(client) AS client, COUNT(*) AS events
FROM events
WHERE trig = 'stop' AND (billing IS NULL OR billing = 'unknown')
GROUP BY email;
```

## 5. What the data cannot say

- `subscription` means *seat-based*. `extra_usage = yes` says a seat can bill beyond its limit, but not how much was billed, and an Enterprise contract can still be usage-based. Invoices and the billing mode live in the Anthropic admin console.
- A set but unused `ANTHROPIC_API_KEY` makes a developer look `metered`.
- The detection runs on each developer's machine, so it is as current as their last report (it is re-checked every 12 hours).
