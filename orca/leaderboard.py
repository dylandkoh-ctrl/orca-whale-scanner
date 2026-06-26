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
