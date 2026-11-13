"""Journal statistics (guide step 5.4-5.5).

Every number is computed from the trades table alone, so it can be checked by
hand. The win rate carries a 95% range because a win rate from 30 trades is
mostly noise: near 50%, the range is about +-17 points at 30 trades and +-10
at 100. The range is the Wilson score interval; it agrees with the plain
p +- 1.96 sqrt(p(1-p)/n) near 50% and, unlike it, does not collapse to zero
width at 0% or 100%.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

Z95 = 1.959964

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

#: Entry hour (UTC) -> trading session. Fixed UTC hours, no daylight saving
#: adjustment; the labels say so.
SESSIONS = [
    ("Asia (22-07 UTC)", lambda h: h >= 22 or h < 7),
    ("London (07-12 UTC)", lambda h: 7 <= h < 12),
    ("London/NY overlap (12-16 UTC)", lambda h: 12 <= h < 16),
    ("New York (16-22 UTC)", lambda h: 16 <= h < 22),
]

SMALL_SAMPLE = 30


def wilson(wins: int, n: int) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = wins / n
    denom = 1 + Z95**2 / n
    centre = (p + Z95**2 / (2 * n)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _entry_dt(trade: dict) -> datetime:
    return datetime.fromtimestamp(trade["entry_ns"] / 1e9, tz=timezone.utc)


def _summary(trades: list[dict]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["net_usd"] > 0]
    losses = [t for t in trades if t["net_usd"] < 0]
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    win_rs = [r for r in rs if r > 0]
    loss_rs = [r for r in rs if r < 0]
    gross_win = sum(t["net_usd"] for t in wins)
    gross_loss = -sum(t["net_usd"] for t in losses)
    ci = wilson(len(wins), n)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": n - len(wins) - len(losses),
        "win_rate": len(wins) / n if n else None,
        "win_rate_ci": list(ci) if ci else None,
        "net_usd": round(sum(t["net_usd"] for t in trades), 2),
        "avg_win_r": round(sum(win_rs) / len(win_rs), 3) if win_rs else None,
        "avg_loss_r": round(sum(loss_rs) / len(loss_rs), 3) if loss_rs else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "trades_with_r": len(rs),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "small_sample": n < SMALL_SAMPLE,
    }


def compute(trades: list[dict], starting_balance: float | None = None) -> dict:
    """Headline numbers, equity curve, drawdown and splits.

    `trades` must be in exit order. With `starting_balance` (one session) the
    curve and drawdown are in account equity; without (several sessions
    pooled) they are cumulative profit from zero.
    """
    out = _summary(trades)

    base = starting_balance or 0.0
    equity = base
    peak = base
    max_dd = 0.0
    max_dd_pct = None
    curve = [{"n": 0, "equity": round(base, 2)}]
    for i, t in enumerate(trades, 1):
        equity += t["net_usd"]
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            if starting_balance:
                max_dd_pct = 100 * dd / peak if peak > 0 else None
        curve.append({"n": i, "equity": round(equity, 2)})
    out["equity_curve"] = curve
    out["curve_kind"] = "equity" if starting_balance else "cumulative profit"
    out["max_drawdown_usd"] = round(max_dd, 2)
    out["max_drawdown_pct"] = round(max_dd_pct, 2) if max_dd_pct is not None else None

    holds = [(t["exit_ns"] - t["entry_ns"]) / 1e9 for t in trades]
    out["avg_hold_seconds"] = round(sum(holds) / len(holds)) if holds else None

    by_session = []
    for label, test in SESSIONS:
        group = [t for t in trades if test(_entry_dt(t).hour)]
        by_session.append({"label": label, **_summary(group)})
    out["by_session"] = by_session

    by_weekday = []
    for d, label in enumerate(WEEKDAYS):
        group = [t for t in trades if _entry_dt(t).weekday() == d]
        if group or d < 5:
            by_weekday.append({"label": label, **_summary(group)})
    out["by_weekday"] = by_weekday

    tags: dict[str, list[dict]] = {}
    for t in trades:
        for tag in filter(None, (t.get("tags") or "").split(",")):
            tags.setdefault(tag, []).append(t)
    out["by_tag"] = [{"label": tag, **_summary(g)} for tag, g in sorted(tags.items())]

    reasons: dict[str, list[dict]] = {}
    for t in trades:
        reasons.setdefault(t["exit_reason"], []).append(t)
    out["by_exit"] = [{"label": k, **_summary(g)} for k, g in sorted(reasons.items())]
    return out
