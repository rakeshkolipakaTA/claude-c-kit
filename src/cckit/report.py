"""`cckit report`: token usage from Claude Code's local session transcripts (~/.claude/projects/**/*.jsonl).

This is monitoring that needs no server: every assistant message in a transcript carries the token counts the API
returned. Only numbers and timestamps are read; no message text is shown or stored. Cost is an ESTIMATE and is only
printed when you give prices in cckit.toml ([prices.<word in model name>]); the real bill is in your admin console.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class Tot:
    requests: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, u: dict) -> None:
        self.requests += 1
        self.input += int(u.get("input_tokens") or 0)
        self.output += int(u.get("output_tokens") or 0)
        self.cache_read += int(u.get("cache_read_input_tokens") or 0)
        self.cache_write += int(u.get("cache_creation_input_tokens") or 0)

    @property
    def total_input(self) -> int:
        return self.input + self.cache_read + self.cache_write

    @property
    def cache_hit(self) -> float | None:
        return self.cache_read / self.total_input if self.total_input else None


@dataclass
class Session:
    sid: str
    project: str
    first: str = ""
    tot: Tot = field(default_factory=Tot)
    side: Tot = field(default_factory=Tot)
    max_context: int = 0
    models: set = field(default_factory=set)


def default_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def collect(base: Path, days: int | None = None, project: str | None = None):
    """Yield one record per API response: (project, session, day, model, sidechain, usage)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    for f in sorted(Path(base).glob("*/*.jsonl")):
        proj = f.parent.name
        if project and project.lower() not in proj.lower():
            continue
        if cutoff and datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
            continue
        last: dict[str, tuple] = {}
        try:
            fh = f.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message") or {}
                model, u = m.get("model") or "", m.get("usage")
                if not isinstance(u, dict) or not model or model.startswith("<"):
                    continue
                ts = _parse_ts(d.get("timestamp"))
                if cutoff and ts and ts < cutoff:
                    continue
                key = m.get("id") or d.get("uuid") or str(len(last))
                # a response is written once per content block with growing counts: the last one is the final total
                last[key] = (proj, d.get("sessionId") or f.stem, (ts.date().isoformat() if ts else ""), model, bool(d.get("isSidechain")), u)
        yield from last.values()


def summarise(records) -> dict:
    by_day_model: dict[tuple, Tot] = defaultdict(Tot)
    sessions: dict[str, Session] = {}
    for proj, sid, day, model, side, u in records:
        by_day_model[(day, model)].add(u)
        s = sessions.setdefault(sid, Session(sid, proj, first=day))
        s.first = min(s.first, day) if s.first and day else (s.first or day)
        (s.side if side else s.tot).add(u)
        s.models.add(model)
        ctx = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
        if not side:
            s.max_context = max(s.max_context, ctx)
    return {"by_day_model": by_day_model, "sessions": sessions}


def price_for(model: str, prices: dict) -> dict | None:
    for word, p in prices.items():
        if word.lower() in model.lower():
            return p
    return None


def estimate_cost(t: Tot, p: dict) -> float:
    return (t.input * p.get("input", 0) + t.output * p.get("output", 0) + t.cache_read * p.get("cache_read", 0) + t.cache_write * p.get("cache_write", 0)) / 1_000_000


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def render(summary: dict, prices: dict | None = None, top: int = 8, context_warn: int = 150_000) -> str:
    prices = prices or {}
    lines: list[str] = []
    bdm = summary["by_day_model"]
    if not bdm:
        return "No Claude Code usage found."
    grand = Tot()
    lines.append("Tokens by day and model (input = fresh + cache read + cache write)")
    head = f"{'day':<11}{'model':<26}{'req':>6}{'input':>10}{'output':>10}{'cache hit':>11}" + (f"{'est. $':>9}" if prices else "")
    lines.append(head)
    total_cost, priced = 0.0, True
    for (day, model), t in sorted(bdm.items()):
        hit = f"{t.cache_hit * 100:.0f}%" if t.cache_hit is not None else "-"
        row = f"{day:<11}{model[:25]:<26}{t.requests:>6}{_k(t.total_input):>10}{_k(t.output):>10}{hit:>11}"
        if prices:
            p = price_for(model, prices)
            if p:
                c = estimate_cost(t, p)
                total_cost += c
                row += f"{c:>9.2f}"
            else:
                priced = False
                row += f"{'?':>9}"
        lines.append(row)
        for a in ("requests", "input", "output", "cache_read", "cache_write"):
            setattr(grand, a, getattr(grand, a) + getattr(t, a))
    hit = f"{grand.cache_hit * 100:.0f}%" if grand.cache_hit is not None else "-"
    lines.append(f"{'TOTAL':<37}{grand.requests:>6}{_k(grand.total_input):>10}{_k(grand.output):>10}{hit:>11}" + (f"{total_cost:>9.2f}" if prices else ""))
    if prices and not priced:
        lines.append("('?' = no [prices.*] entry matches that model; the total leaves it out)")
    sess = sorted(summary["sessions"].values(), key=lambda s: s.tot.total_input + s.side.total_input + s.tot.output + s.side.output, reverse=True)[:top]
    lines.append("")
    lines.append(f"Heaviest sessions (top {len(sess)})")
    lines.append(f"{'session':<10}{'start':<12}{'req':>6}{'max ctx':>9}{'output':>9}{'subagent req':>14}")
    for s in sess:
        flag = "  <- long context: /clear or /compact earlier" if s.max_context >= context_warn else ""
        lines.append(f"{s.sid[:8]:<10}{s.first:<12}{s.tot.requests:>6}{_k(s.max_context):>9}{_k(s.tot.output + s.side.output):>9}{s.side.requests:>14}{flag}")
    return "\n".join(lines)
