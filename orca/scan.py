"""One refresh = one scan. Ties the pipeline together for the UI.

discover today's match markets -> holders -> price-to-USD -> triggers A/B/C ->
profile flagged wallets -> persist -> return a result bundle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from . import config, discovery, holders, profiling, store, triggers
from .profiling import WalletProfile
from .triggers import Flag


@dataclass
class ScanResult:
    scan_ts: int
    date: str
    markets: list[dict[str, Any]]
    holders: pd.DataFrame
    flags: list[Flag]
    profiles: dict[str, WalletProfile]
    names: dict[str, str] = field(default_factory=dict)

    @property
    def consensus(self) -> pd.DataFrame:
        """Bets ranked by combined whale $ and # distinct whales."""
        if not self.flags:
            return pd.DataFrame()
        df = triggers.flags_to_frame(self.flags)
        return df.sort_values(["total_usd", "n_accounts"], ascending=False)


def run_scan(date: str | None = None,
             thresholds: dict[str, float] | None = None,
             groups: dict[str, bool] | None = None,
             conn=None) -> ScanResult:
    """Run a full scan for `date` (default today). Threshold/group overrides from UI."""
    t = thresholds or {}
    scan_ts = int(time.time())

    markets = discovery.discover_matches(date=date, groups=groups)
    date = date or discovery._today_iso()
    holders_df = holders.build_holders_frame(markets)

    flags = triggers.evaluate(
        holders_df,
        single_whale_usd=t.get("single_whale_usd", config.SINGLE_WHALE_USD),
        combined_usd=t.get("combined_usd", config.COMBINED_USD),
        noise_floor_usd=t.get("noise_floor_usd", config.NOISE_FLOOR_USD),
        cluster_wallet_usd=t.get("cluster_wallet_usd", config.CLUSTER_WALLET_USD),
        cluster_min_wallets=int(t.get("cluster_min_wallets", config.CLUSTER_MIN_WALLETS)),
    )

    # Profile only the largest flagged wallets (by total exposure across the
    # day's markets) — profiling all 100+ small holders is what made scans slow.
    flagged = {w for f in flags for w in f.wallets}
    if flagged and not holders_df.empty:
        ranked = (holders_df[holders_df["wallet"].isin(flagged)]
                  .groupby("wallet")["usd"].sum()
                  .sort_values(ascending=False))
        to_profile = list(ranked.head(config.PROFILE_MAX_WALLETS).index)
    else:
        to_profile = []
    profiles = profiling.profile_wallets(to_profile)

    names: dict[str, str] = {}
    if not holders_df.empty:
        for _, row in holders_df.iterrows():
            if row["wallet"] and row["display_name"]:
                names.setdefault(row["wallet"], row["display_name"])

    if conn is not None:
        store.persist_scan(conn, flags, profiles, names, scan_ts)

    return ScanResult(
        scan_ts=scan_ts, date=date, markets=markets, holders=holders_df,
        flags=flags, profiles=profiles, names=names,
    )
