"""Pull /holders per market and build the wallet x bet x USD table.

Verified shape: /holders returns a list grouped *by token*:
    [{"token": "...", "holders": [{proxyWallet, amount, outcomeIndex, ...}]}]
`amount` is in SHARES. We price each holder to USD using the market's current
outcome price (already pulled from Gamma — no extra CLOB call needed).

A "bet" here is one side of one market, e.g. (United States: No) or (O/U 2.5: Over).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import config
from .api import get_json, parallel_map, to_float

HOLDERS_COLUMNS = [
    "condition_id", "match_title", "match_time", "group", "question",
    "bet_group", "outcome_index", "outcome", "bet_label",
    "wallet", "display_name", "shares", "price", "usd",
]


def fetch_market_holders(market: dict[str, Any], limit: int | None = None) -> list[dict]:
    """Fetch and flatten holders for one market into row dicts (one per holder)."""
    limit = limit or config.HOLDERS_LIMIT
    raw = get_json(
        config.DATA_HOST, "/holders",
        params={"market": market["condition_id"], "limit": limit},
        ttl=config.HOLDERS_TTL,
    )

    # token id -> (outcome_index, label, price, bet_label)
    token_meta = {
        str(tid): (
            i,
            market["outcomes"][i] if i < len(market["outcomes"]) else str(i),
            market["prices"][i] if i < len(market["prices"]) else 0.0,
            market["bet_labels"][i] if i < len(market["bet_labels"]) else "?",
        )
        for i, tid in enumerate(market["token_ids"])
    }

    rows: list[dict] = []
    for group in raw or []:
        idx, label, price, bet_label = token_meta.get(
            str(group.get("token")), (None, "?", 0.0, "?"))
        for h in group.get("holders", []):
            shares = to_float(h.get("amount"))
            # Prefer a real handle. Polymarket defaults `name` to
            # "<wallet>-<timestamp>" for users who never set one — treat that
            # (and any 0x-prefixed value) as "no name" and fall back to pseudonym.
            name = (h.get("name") or "").strip()
            if name.lower().startswith("0x"):
                name = ""
            display_name = name or (h.get("pseudonym") or "").strip()
            rows.append({
                "condition_id": market["condition_id"],
                "match_title": market["match_title"],
                "match_time": market["match_time"],
                "group": market["group"],
                "question": market["question"],
                "bet_group": market["bet_group"],
                "outcome_index": h.get("outcomeIndex", idx),
                "outcome": label,
                "bet_label": bet_label,
                "wallet": h.get("proxyWallet"),
                "display_name": display_name,
                "shares": shares,
                "price": price,
                "usd": shares * price,
            })
    return rows


def build_holders_frame(markets: list[dict[str, Any]]) -> pd.DataFrame:
    """Holders across all markets as one tidy DataFrame (wallet x bet x USD)."""
    # One /holders call per market, fanned out concurrently.
    rows: list[dict] = []
    for sub in parallel_map(fetch_market_holders, markets):
        rows.extend(sub)
    if not rows:
        return pd.DataFrame(columns=HOLDERS_COLUMNS)
    return pd.DataFrame(rows, columns=HOLDERS_COLUMNS)
