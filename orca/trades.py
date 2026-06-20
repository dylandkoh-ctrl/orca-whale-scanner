"""Trigger D — live large prints from /trades.

Where holders/triggers A-C read standing *exposure* (a snapshot of who holds
what), this reads *flow*: individual fills as they land. We flag any single
trade at or above a USD threshold (default $300K) on one of the day's match
markets — the earliest possible signal, before it settles into /holders.

Key API facts (verified):
  * /trades?filterType=CASH&filterAmount=N filters SERVER-SIDE to fills >= $N.
  * Without a `market` param it returns these globally, newest first; we keep
    only the ones on today's match conditionIds.
  * limit > ~100 times out, so we page with limit=100 + offset.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import config
from .api import get_json, to_float

PRINT_COLUMNS = [
    "timestamp", "match_title", "bet_label", "side", "size", "price", "usd",
    "wallet", "display_name", "tx",
]


def _clean_name(t: dict) -> str:
    name = (t.get("name") or "").strip()
    if name.lower().startswith("0x"):
        name = ""
    return name or (t.get("pseudonym") or "").strip()


def fetch_large_prints(markets: list[dict[str, Any]],
                       min_usd: float = config.LARGE_PRINT_USD,
                       pages: int = config.TRADES_PAGES,
                       ttl: float = config.TRADES_TTL) -> pd.DataFrame:
    """Recent fills >= `min_usd` on the given day's match markets, newest first.

    `ttl=0` forces a fresh fetch (used by the "check for new prints" button).
    """
    by_cid = {m["condition_id"]: m for m in markets}
    limit = config.TRADES_PAGE_LIMIT
    rows: list[dict] = []

    for p in range(pages):
        try:
            data = get_json(
                config.DATA_HOST, "/trades",
                params={"limit": limit, "offset": p * limit,
                        "filterType": "CASH", "filterAmount": int(min_usd)},
                ttl=ttl,
            )
        except RuntimeError:
            break  # the filtered feed sometimes times out on deeper pages
        if not data:
            break
        for t in data:
            m = by_cid.get(t.get("conditionId"))
            if not m:
                continue  # not one of today's match markets
            oi = t.get("outcomeIndex")
            labels = m["bet_labels"]
            bet = (labels[oi] if isinstance(oi, int) and oi < len(labels)
                   else f"{t.get('title', '')}: {t.get('outcome', '')}")
            size = to_float(t.get("size"))
            price = to_float(t.get("price"))
            rows.append({
                "timestamp": t.get("timestamp"),
                "match_title": m["match_title"],
                "bet_label": bet,
                "side": t.get("side"),          # BUY / SELL — direction matters
                "size": size,
                "price": price,
                "usd": size * price,
                "wallet": t.get("proxyWallet"),
                "display_name": _clean_name(t),
                "tx": t.get("transactionHash"),
            })
        if len(data) < limit:
            break  # reached the end of the filtered feed

    if not rows:
        return pd.DataFrame(columns=PRINT_COLUMNS)
    return (pd.DataFrame(rows, columns=PRINT_COLUMNS)
            .sort_values("timestamp", ascending=False)
            .reset_index(drop=True))
