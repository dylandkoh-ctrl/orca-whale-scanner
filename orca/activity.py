"""Per-wallet track record: recent bets + resolved win rate, from /activity.

One /activity pull yields both — TRADE rows are recent bets, REDEEM rows mark
resolved markets (won if net realized proceeds > cost). Combined with the
7-day P/L from user-pnl for a quick "is this wallet sharp?" scouting card.
"""
from __future__ import annotations

import pandas as pd

from . import config
from .api import get_json, to_float
from .leaderboard import weekly_pnl

RECENT_COLUMNS = ["timestamp", "market", "pick", "side", "usd"]


def _activity(wallet: str, depth: int) -> list[dict]:
    return get_json(config.DATA_HOST, "/activity",
                    params={"user": wallet, "limit": depth},
                    ttl=config.PROFILE_TTL) or []


def realized_record(acts: list[dict]) -> tuple[int, float, float]:
    """(resolved_market_count, win_rate, realized_pnl) from activity items.

    A market is resolved if it has a REDEEM; a win if net (SELL+REDEEM proceeds
    − BUY cost) > 0. Approximate: bounded to the activity window, and cost is
    undercounted if a position was opened before it.
    """
    by_cid: dict[str, dict] = {}
    redeemed: set[str] = set()
    for a in acts:
        cid = a.get("conditionId")
        if not cid:
            continue
        usd = to_float(a.get("usdcSize"))
        d = by_cid.setdefault(cid, {"cost": 0.0, "proceeds": 0.0})
        if a.get("type") == "TRADE":
            if a.get("side") == "BUY":
                d["cost"] += usd
            elif a.get("side") == "SELL":
                d["proceeds"] += usd
        elif a.get("type") == "REDEEM":
            d["proceeds"] += usd
            redeemed.add(cid)
    n = len(redeemed)
    wins = sum(1 for c in redeemed if by_cid[c]["proceeds"] > by_cid[c]["cost"])
    realized = sum(by_cid[c]["proceeds"] - by_cid[c]["cost"] for c in redeemed)
    return n, (wins / n if n else 0.0), realized


def recent_bets(acts: list[dict], limit: int = 12) -> pd.DataFrame:
    """Most recent TRADE fills as a tidy DataFrame."""
    rows = [{
        "timestamp": a.get("timestamp"),
        "market": a.get("title", ""),
        "pick": a.get("outcome", ""),
        "side": a.get("side"),
        "usd": to_float(a.get("usdcSize")),
    } for a in acts if a.get("type") == "TRADE"][:limit]
    return pd.DataFrame(rows, columns=RECENT_COLUMNS)


def track_record(wallet: str, depth: int = 1000) -> dict:
    """Bundle: 7-day P/L, resolved win rate + count, and recent bets."""
    acts = _activity(wallet, depth)
    n, wr, realized = realized_record(acts)
    return {
        "week_pnl": weekly_pnl(wallet),
        "resolved_n": n,
        "win_rate": wr,
        "realized_pnl": realized,
        "recent": recent_bets(acts),
    }
