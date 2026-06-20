"""Triggers A/B/C over the holders table.

The unit of a "flag" is one *bet* — one side of one market, e.g.
(United States: No) or (O/U 2.5: Over). Both sides of every market are
directional here (unlike tournament futures), so nothing is filtered out.

  A — Single-whale:  any one wallet >= SINGLE_WHALE_USD on the bet.
  B — Combined:      sum of wallets each >= NOISE_FLOOR_USD reaches COMBINED_USD.
  C — Clustering:    >= CLUSTER_MIN_WALLETS wallets each >= CLUSTER_WALLET_USD.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import config


@dataclass
class Flag:
    condition_id: str
    match_title: str
    match_time: str
    group: str
    question: str
    bet_label: str                  # "United States: No"
    outcome_index: int
    triggers: list[str] = field(default_factory=list)   # e.g. ["A", "C"]
    total_usd: float = 0.0          # qualifying combined exposure (>= noise floor)
    n_accounts: int = 0
    top_usd: float = 0.0            # largest single wallet on this bet
    wallets: list[str] = field(default_factory=list)    # wallets to profile/watch


def evaluate(
    holders: pd.DataFrame,
    single_whale_usd: float = config.SINGLE_WHALE_USD,
    combined_usd: float = config.COMBINED_USD,
    noise_floor_usd: float = config.NOISE_FLOOR_USD,
    cluster_wallet_usd: float = config.CLUSTER_WALLET_USD,
    cluster_min_wallets: int = config.CLUSTER_MIN_WALLETS,
) -> list[Flag]:
    """Return one Flag per bet (market, outcome) that trips A, B, or C."""
    if holders.empty:
        return []

    key_cols = ["condition_id", "match_title", "match_time", "group",
                "question", "bet_label", "outcome_index"]
    # Collapse to one row per (bet, wallet) first.
    per_wallet = holders.groupby(key_cols + ["wallet"], as_index=False)["usd"].sum()

    flags: list[Flag] = []
    for keys, grp in per_wallet.groupby(key_cols):
        cid, match_title, match_time, group, question, bet_label, oi = keys
        grp = grp.sort_values("usd", ascending=False)

        top_usd = float(grp["usd"].max())
        qualifying = grp[grp["usd"] >= noise_floor_usd]
        clustered = grp[grp["usd"] >= cluster_wallet_usd]
        combined = float(qualifying["usd"].sum())

        fired: list[str] = []
        if top_usd >= single_whale_usd:
            fired.append("A")
        if combined >= combined_usd and len(qualifying) > 0:
            fired.append("B")
        if len(clustered) >= cluster_min_wallets:
            fired.append("C")
        if not fired:
            continue

        flags.append(Flag(
            condition_id=cid, match_title=match_title, match_time=match_time,
            group=group, question=question, bet_label=bet_label,
            outcome_index=int(oi), triggers=fired,
            total_usd=combined, n_accounts=int(len(qualifying)),
            top_usd=top_usd, wallets=qualifying["wallet"].tolist(),
        ))

    flags.sort(key=lambda f: f.total_usd, reverse=True)
    return flags


def flags_to_frame(flags: list[Flag]) -> pd.DataFrame:
    return pd.DataFrame([{
        "match_title": f.match_title,
        "bet_label": f.bet_label,
        "group": f.group,
        "triggers": "+".join(f.triggers),
        "total_usd": f.total_usd,
        "n_accounts": f.n_accounts,
        "top_usd": f.top_usd,
        "condition_id": f.condition_id,
    } for f in flags])
