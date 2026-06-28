"""Weekly P/L leaderboard for the whales we observe making large WC bets.

There is no public global Polymarket leaderboard endpoint (it 404s), but the
per-wallet P/L chart endpoint works:

    GET user-pnl-api.polymarket.com/user-pnl?user_address=W&interval=1w&fidelity=1d
      -> [{"t": <unix>, "p": <cumulative P/L $>}, ...]  (daily points over a week)

So a wallet's 7-day P/L is simply the last point minus the first. We rank the
universe of wallets passed in (the big WC bettors) by that delta.
"""
from __future__ import annotations

import pandas as pd

from . import config
from .api import get_json, parallel_map, to_float


def weekly_pnl(wallet: str) -> float | None:
    """A wallet's profit over the last ~7 days, or None if no P/L history."""
    series = get_json(
        config.USER_PNL_HOST, "/user-pnl",
        params={"user_address": wallet, "interval": "1w", "fidelity": "1d"},
        ttl=config.PROFILE_TTL,
    )
    if not series or len(series) < 2:
        return None
    return to_float(series[-1].get("p")) - to_float(series[0].get("p"))


def weekly_leaderboard(wallets: list[str],
                       names: dict[str, str] | None = None) -> pd.DataFrame:
    """Rank `wallets` by 7-day P/L, descending. Columns: wallet, name, week_pnl."""
    names = names or {}
    uniq = [w for w in dict.fromkeys(wallets) if w]
    pnls = parallel_map(weekly_pnl, uniq)
    rows = [{"wallet": w, "name": names.get(w, ""), "week_pnl": p}
            for w, p in zip(uniq, pnls) if p is not None]
    if not rows:
        return pd.DataFrame(columns=["wallet", "name", "week_pnl"])
    return (pd.DataFrame(rows)
            .sort_values("week_pnl", ascending=False)
            .reset_index(drop=True))


# --- realized record + blended "smart money" score ----------------------
# Shrinkage prior for the win rate: until a wallet has a real sample of resolved
# markets, its win rate is pulled toward PRIOR_WR. PRIOR_N is the pseudo-count —
# higher = more skeptical of small samples (this is the "weight by sample size,
# not headline" knob).
PRIOR_N = 8.0
PRIOR_WR = 0.5
ACTIVITY_DEPTH = 1000   # /activity items to scan for resolved markets


def realized_record(wallet: str, depth: int = ACTIVITY_DEPTH) -> tuple[int, float]:
    """(resolved_market_count, realized_win_rate) from recent /activity.

    A market counts as resolved if the wallet has a REDEEM on it; it's a win if
    net realized (SELL + REDEEM proceeds − BUY cost) is positive. Approximate:
    bounded to the last `depth` activity items, and cost can be undercounted if
    a position was opened before the window.
    """
    acts = get_json(config.DATA_HOST, "/activity",
                    params={"user": wallet, "limit": depth}, ttl=config.PROFILE_TTL) or []
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
    return n, (wins / n if n else 0.0)


def smart_money_leaderboard(wallets: list[str],
                            names: dict[str, str] | None = None) -> pd.DataFrame:
    """Rank wallets by 7-day P/L scaled by a sample-size-shrunk realized win rate.

    score = week_pnl × shrunk_win_rate, where
      shrunk_win_rate = (wins + PRIOR_N·PRIOR_WR) / (resolved_n + PRIOR_N)
    so a big P/L on a thin or poor resolved record is discounted, and a strong,
    well-sampled win rate is rewarded. Columns: wallet, name, week_pnl,
    resolved_n, win_rate, score.
    """
    names = names or {}
    uniq = [w for w in dict.fromkeys(wallets) if w]
    pnls = parallel_map(weekly_pnl, uniq)
    records = parallel_map(realized_record, uniq)
    rows = []
    for w, p, (n, wr) in zip(uniq, pnls, records):
        if p is None:
            continue
        shrunk = (wr * n + PRIOR_N * PRIOR_WR) / (n + PRIOR_N)
        rows.append({"wallet": w, "name": names.get(w, ""), "week_pnl": p,
                     "resolved_n": n, "win_rate": wr, "score": p * shrunk})
    if not rows:
        return pd.DataFrame(columns=["wallet", "name", "week_pnl",
                                     "resolved_n", "win_rate", "score"])
    return (pd.DataFrame(rows)
            .sort_values("score", ascending=False)
            .reset_index(drop=True))
