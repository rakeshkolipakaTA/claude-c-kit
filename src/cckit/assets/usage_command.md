---
description: Show token usage and estimated cost for this session and the last 7 days
allowed-tools: Bash(python3 .cckit/scripts/usage_report.py:*)
---
This session:
!`python3 .cckit/scripts/usage_report.py`

Last 7 days (this project):
!`python3 .cckit/scripts/usage_report.py --session all --days 7`

Summarise the two reports above in at most 6 lines: tokens, cache hit, the cost if one is shown, context used now, and one concrete suggestion to spend fewer tokens. Do not run anything else.
