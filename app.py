"""Orca — World Cup Whale Scanner (Streamlit UI).

Tracks large single-match bets for a given day — moneyline ("USA Win: No"),
totals ("O/U 2.5: Over"), both-teams-to-score, spreads — and the sharp wallets
behind them.

Three tabs:
  Flagged Now     — flagged bets grouped by match, + the resolution fine print.
  Watchlist       — the growing roster of flagged wallets, graded.
  Consensus Board — bets ranked by combined whale $ and # distinct whales.

Run:  streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd
import streamlit as st

from orca import config, discovery, profiling, store
from orca.api import get_json, parallel_map
from orca.scan import run_scan

st.set_page_config(page_title="Orca — Whale Scanner", page_icon="🐋", layout="wide")


@st.cache_resource
def get_conn():
    conn = store.connect(config.DB_PATH)
    store.init_db(conn)
    return conn


@st.cache_data(ttl=config.DISCOVERY_TTL)
def match_days():
    try:
        return discovery.list_match_days(limit_days=10)
    except Exception:
        return []


conn = get_conn()

# --- sidebar ------------------------------------------------------------
st.sidebar.title("🐋 Orca")
st.sidebar.caption("World Cup single-match whale scanner")

# Date picker — defaults to today, but offer the real upcoming match days.
days = match_days()
today = dt.date.today().isoformat()
default_idx = days.index(today) if today in days else 0
date = st.sidebar.selectbox(
    "Match day", options=days or [today],
    index=default_idx if days else 0,
)

st.sidebar.subheader("Market families")
groups = {g: st.sidebar.checkbox(g, value=default)
          for g, default in config.MARKET_GROUPS.items()}

top_n = st.sidebar.number_input("Top positions to show", value=5, min_value=1,
                                max_value=50, step=1)

st.sidebar.subheader("Detection thresholds")
thresholds = {
    "single_whale_usd": st.sidebar.number_input(
        "A · Single wallet ≥ $", value=config.SINGLE_WHALE_USD, step=25_000),
    "combined_usd": st.sidebar.number_input(
        "B · Combined ≥ $", value=config.COMBINED_USD, step=25_000),
    "noise_floor_usd": st.sidebar.number_input(
        "B · Noise floor (per wallet) ≥ $", value=config.NOISE_FLOOR_USD, step=5_000),
    "cluster_wallet_usd": st.sidebar.number_input(
        "C · Cluster wallet ≥ $", value=config.CLUSTER_WALLET_USD, step=5_000),
    "cluster_min_wallets": st.sidebar.number_input(
        "C · Cluster min wallets", value=config.CLUSTER_MIN_WALLETS, step=1),
}

st.sidebar.divider()
run = st.sidebar.button("🔄 Refresh scan", type="primary", width="stretch")


# --- run a scan on demand ------------------------------------------------
if run or "result" not in st.session_state or st.session_state.get("date") != date:
    with st.spinner(f"Scanning {date} matches…"):
        st.session_state["result"] = run_scan(
            date=date, thresholds=thresholds, groups=groups, conn=conn)
        st.session_state["date"] = date

result = st.session_state["result"]
matches = sorted({m["match_title"] for m in result.markets})
st.title("Orca — World Cup Whale Scanner")
st.caption(
    f"**{result.date}** · {len(matches)} matches · {len(result.markets)} markets · "
    f"{len(result.flags)} flagged bets · scanned "
    f"{time.strftime('%H:%M:%S', time.localtime(result.scan_ts))}"
)
if matches:
    st.caption("Matches: " + " · ".join(matches))

PROFILE_BASE = "https://polymarket.com/profile/"

tab_top, tab_flagged, tab_watch, tab_consensus = st.tabs(
    ["🐋 Top Positions", "🚩 Flagged Now", "📋 Watchlist", "📊 Consensus Board"]
)


def _grade_badge(p) -> str:
    if p is None:
        return "—"
    return f"{p.grade}/100" + (" · ⚠️ MM/HEDGE" if p.is_mm_hedge else "")


# --- Top Positions -------------------------------------------------------
with tab_top:
    h = result.holders
    if h.empty:
        st.info("No positions found for this day. Try another match day or refresh.")
    else:
        st.caption(f"The {int(top_n)} largest single positions across "
                   f"{result.date}'s World Cup games — by USD exposure.")

        hide_mm = st.checkbox(
            f"Hide system / market-maker wallets (portfolio > ${config.MAKER_VALUE_USD/1e6:.0f}M)",
            value=True,
            help="The negRisk maker holds every outcome and has an empty profile "
                 "(~$16B balance). Real whales are single-digit millions.")

        # Rank by size, then resolve each candidate wallet's portfolio value to
        # tell real bettors from the system maker. Most are already profiled in
        # the scan (free); fetch /value only for the few that aren't (cached).
        cand = h.sort_values("usd", ascending=False).head(max(int(top_n) * 5, 60)).copy()

        # Reuse values already computed during profiling; fetch the rest in
        # parallel so this stays fast even with a wide candidate set.
        uniq = list(cand["wallet"].unique())
        values = {w: result.profiles[w].value_usd
                  for w in uniq if w in result.profiles}
        missing = [w for w in uniq if w not in values]

        def fetch_value(w: str) -> tuple[str, float]:
            v = get_json(config.DATA_HOST, "/value", {"user": w}, ttl=config.PROFILE_TTL)
            return w, (float(v[0]["value"]) if v else 0.0)

        values.update(dict(parallel_map(fetch_value, missing)))
        cand["wallet_value"] = cand["wallet"].map(values)
        cand["is_system"] = cand["wallet_value"] > config.MAKER_VALUE_USD

        # "legs" = how many of the match's markets this wallet sits in (info only).
        legs = (h.groupby(["wallet", "match_title"])["condition_id"]
                  .nunique().rename("legs").reset_index())
        cand = cand.merge(legs, on=["wallet", "match_title"], how="left")

        if hide_mm:
            cand = cand[~cand["is_system"]]

        top = cand.sort_values("usd", ascending=False).head(int(top_n)).copy()
        top.insert(0, "rank", range(1, len(top) + 1))
        top["user"] = top.apply(
            lambda r: r["display_name"] or f"{r['wallet'][:6]}…{r['wallet'][-4:]}", axis=1)

        # Per-position entry/current detail (cost basis vs market value), pulled
        # from /positions. Cached per wallet so multi-leg wallets don't refetch.
        _detail: dict[str, dict] = {}

        def pos_detail(wallet: str, cid: str, oi) -> dict:
            if wallet not in _detail:
                _detail[wallet] = profiling.position_details(wallet)
            return _detail[wallet].get((cid, oi), {})

        table_view = st.toggle("Table view", value=False,
                               help="Compact table instead of cards (handy on desktop).")

        if table_view:
            det = [pos_detail(r.wallet, r.condition_id, r.outcome_index)
                   for r in top.itertuples()]
            top["buy_price"] = [d.get("avg_price") for d in det]
            top["entry_cost"] = [d.get("initial_value") for d in det]
            top["current_value"] = [d.get("current_value") or u
                                    for d, u in zip(det, top["usd"])]
            top["pnl"] = [d.get("cash_pnl") for d in det]
            top["profile"] = PROFILE_BASE + top["wallet"]
            st.dataframe(
                top[["rank", "match_title", "bet_label", "user", "buy_price",
                     "entry_cost", "current_value", "pnl", "wallet_value",
                     "legs", "profile"]],
                width="stretch", hide_index=True,
                column_config={
                    "rank": st.column_config.NumberColumn("#", width="small"),
                    "match_title": st.column_config.TextColumn("match"),
                    "bet_label": st.column_config.TextColumn("bet"),
                    "buy_price": st.column_config.NumberColumn("buy $/sh", format="$%.3f"),
                    "entry_cost": st.column_config.NumberColumn("entry cost", format="$%.0f"),
                    "current_value": st.column_config.NumberColumn("current value", format="$%.0f"),
                    "pnl": st.column_config.NumberColumn("PnL", format="$%.0f"),
                    "wallet_value": st.column_config.NumberColumn("portfolio", format="$%.0f"),
                    "legs": st.column_config.NumberColumn("legs", width="small"),
                    "profile": st.column_config.LinkColumn("profile", display_text="View ↗"),
                },
            )
        else:
            # Mobile-friendly cards: entry (cost basis) vs current (market value).
            for r in top.itertuples():
                d = pos_detail(r.wallet, r.condition_id, r.outcome_index)
                avg_price = d.get("avg_price") or 0.0
                entry_cost = d.get("initial_value") or 0.0
                cur_price = d.get("cur_price") or r.price
                cur_value = d.get("current_value") or r.usd
                pnl = d.get("cash_pnl")
                with st.container(border=True):
                    sys_tag = " · ⚠️ system/MM" if r.is_system else ""
                    st.markdown(f"**#{r.rank} · {r.bet_label}**  ·  {r.match_title}")
                    c0, c1 = st.columns([3, 2])
                    c0.markdown(f"👤 **{r.user}**")
                    c0.caption(f"portfolio ${r.wallet_value:,.0f} · {int(r.legs)} legs{sys_tag}")
                    c1.link_button("Polymarket ↗", PROFILE_BASE + r.wallet,
                                   width="stretch")
                    m1, m2 = st.columns(2)
                    m1.metric("Entry (cost)",
                              f"${entry_cost:,.0f}" if entry_cost else "—")
                    m2.metric("Current value", f"${cur_value:,.0f}",
                              delta=(f"${pnl:,.0f}" if pnl is not None else None))
                    st.caption(f"Buy ${avg_price:.3f} → now ${cur_price:.3f} per share  ·  "
                               f"{int(r.shares):,} shares")


# --- Flagged Now (grouped by match) -------------------------------------
with tab_flagged:
    if not result.flags:
        st.info("No flagged bets at current thresholds. Lower them in the sidebar.")
    # Group flags by match.
    by_match: dict[str, list] = {}
    for f in result.flags:
        by_match.setdefault(f.match_title, []).append(f)

    for match_title in sorted(by_match, key=lambda m: -sum(f.total_usd for f in by_match[m])):
        flags = by_match[match_title]
        st.subheader(f"⚽ {match_title}")
        for f in flags:
            best = max((result.profiles.get(w) for w in f.wallets if result.profiles.get(w)),
                       key=lambda p: p.grade, default=None)
            header = (f"**{f.bet_label}**  ·  `{'+'.join(f.triggers)}`  ·  "
                      f"${f.total_usd:,.0f} across {f.n_accounts} accts  ·  "
                      f"top ${f.top_usd:,.0f}  ·  best grade {_grade_badge(best)}")
            with st.expander(header):
                rows = []
                for w in f.wallets:
                    p = result.profiles.get(w)
                    rows.append({
                        "wallet": w,
                        "name": result.names.get(w, ""),
                        "grade": p.grade if p else None,
                        "MM/hedge": "⚠️" if (p and p.is_mm_hedge) else "",
                        "realized_pnl": p.realized_pnl if p else None,
                        "value_usd": p.value_usd if p else None,
                    })
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                             column_config={
                                 "grade": st.column_config.ProgressColumn(
                                     "grade", min_value=0, max_value=100),
                                 "realized_pnl": st.column_config.NumberColumn(format="$%.0f"),
                                 "value_usd": st.column_config.NumberColumn(format="$%.0f"),
                             })
                mkt = next((m for m in result.markets if m["condition_id"] == f.condition_id), None)
                st.caption("**Resolution rule** — read the fine print:")
                st.write(mkt["resolution_text"] if mkt else "—")
        st.divider()


# --- Watchlist -----------------------------------------------------------
with tab_watch:
    wl = store.watchlist_frame(conn)
    if wl.empty:
        st.info("Watchlist is empty — run a scan to populate it.")
    else:
        now = int(time.time())
        wl["seen"] = wl["first_seen"].apply(
            lambda ts: "🆕 new" if (now - ts) < 120 else "↩️ returning")
        wl["first_seen"] = pd.to_datetime(wl["first_seen"], unit="s")
        wl["last_seen"] = pd.to_datetime(wl["last_seen"], unit="s")
        wl["is_mm_hedge"] = wl["is_mm_hedge"].map({1: "⚠️", 0: ""})
        st.dataframe(
            wl[["wallet", "display_name", "grade", "is_mm_hedge", "realized_pnl",
                "value_usd", "win_rate", "concentration", "seen", "first_seen", "last_seen"]],
            width="stretch", hide_index=True,
            column_config={
                "grade": st.column_config.ProgressColumn("grade", min_value=0, max_value=100),
                "realized_pnl": st.column_config.NumberColumn(format="$%.0f"),
                "value_usd": st.column_config.NumberColumn(format="$%.0f"),
                "win_rate": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )
        st.caption("Win rate / concentration are from each wallet's current open book "
                   "(the leaderboard endpoint is gone) — an approximation of skill, "
                   "not full lifetime history.")


# --- Consensus Board -----------------------------------------------------
with tab_consensus:
    cons = result.consensus
    if cons.empty:
        st.info("Nothing on the consensus board yet.")
    else:
        st.caption("Which bets is the money piling onto — ranked by combined whale $ "
                   "and # distinct whales.")
        st.dataframe(
            cons[["match_title", "bet_label", "group", "triggers",
                  "total_usd", "n_accounts", "top_usd"]],
            width="stretch", hide_index=True,
            column_config={
                "total_usd": st.column_config.NumberColumn("combined $", format="$%.0f"),
                "top_usd": st.column_config.NumberColumn("largest wallet $", format="$%.0f"),
                "n_accounts": st.column_config.NumberColumn("# whales"),
            },
        )
