"""Orca — World Cup Whale Scanner (Streamlit UI).

Three tabs:
  Top Positions    — the largest single-match bets for a chosen day (entry vs
                     current value), with the wallet behind each.
  Weekly Leaderboard — 7-day P/L of the whales making large World Cup bets.
  Largest Bets     — history of the biggest WC match bets and who made them.

Run:  streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd
import streamlit as st

from orca import config, discovery, leaderboard, profiling, store, trades
from orca.api import get_json, parallel_map
from orca.scan import run_scan

st.set_page_config(page_title="Orca — Whale Scanner", page_icon="🐋", layout="wide")


@st.cache_resource
def get_conn():
    conn = store.connect(config.DB_PATH)
    store.init_db(conn)
    return conn


@st.cache_data(ttl=config.DISCOVERY_TTL)
def match_days(tz):
    try:
        return discovery.list_match_days(limit_days=10, tz=tz)
    except Exception:
        return []


conn = get_conn()

# The viewer's timezone (from their browser). Streamlit Cloud servers run in UTC,
# so without this "today's games" would be computed in UTC and drop evening matches.
try:
    user_tz = st.context.timezone
except Exception:
    user_tz = None

# --- sidebar ------------------------------------------------------------
st.sidebar.title("🐋 Orca")
st.sidebar.caption("World Cup single-match whale scanner")

# Date picker — defaults to today (in the viewer's tz), offering real match days.
days = match_days(user_tz)
today = discovery._today_iso(user_tz)
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
large_bet_usd = st.sidebar.number_input(
    "Large bet ≥ $ (leaderboard & history)", value=config.LARGE_PRINT_USD, step=50_000)

st.sidebar.divider()
run = st.sidebar.button("🔄 Refresh scan", type="primary", width="stretch")


# --- run a scan on demand ------------------------------------------------
if run or "result" not in st.session_state or st.session_state.get("date") != date:
    with st.spinner(f"Scanning {date} matches…"):
        st.session_state["result"] = run_scan(
            date=date, groups=groups, conn=conn, tz=user_tz)
        st.session_state["date"] = date

result = st.session_state["result"]
matches = sorted({m["match_title"] for m in result.markets})
st.title("Orca — World Cup Whale Scanner")
st.caption(
    f"**{result.date}** · {len(matches)} matches · {len(result.markets)} markets · "
    f"scanned {time.strftime('%H:%M:%S', time.localtime(result.scan_ts))}"
)
if matches:
    st.caption("Matches: " + " · ".join(matches))

PROFILE_BASE = "https://polymarket.com/profile/"


def _local_dt(ts):
    """Unix seconds -> tz-aware datetime in the viewer's timezone (UTC fallback)."""
    s = pd.to_datetime(ts, unit="s", utc=True)
    try:
        return s.dt.tz_convert(user_tz) if user_tz else s
    except Exception:
        return s


tab_top, tab_lead, tab_history = st.tabs(
    ["🐋 Top Positions", "🏆 Weekly Leaderboard", "📜 Largest Bets"]
)


def _short(w: str) -> str:
    return f"{w[:6]}…{w[-4:]}" if w and len(w) > 12 else (w or "")


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

# --- Weekly Leaderboard --------------------------------------------------
with tab_lead:
    st.caption(f"7-day profit of wallets making large (≥ ${large_bet_usd:,.0f}) "
               "World Cup bets. Polymarket has no public global leaderboard, so this "
               "ranks the whales the scanner sees by their P/L over the last week.")
    wc_prints = trades.fetch_wc_large_prints(min_usd=large_bet_usd)
    if wc_prints.empty:
        st.info("No large WC bets found recently — lower the threshold in the sidebar.")
    else:
        names = {r.wallet: r.display_name
                 for r in wc_prints.itertuples() if r.display_name}
        lb = leaderboard.weekly_leaderboard(wc_prints["wallet"].tolist(), names)
        if lb.empty:
            st.info("No P/L history available for these wallets yet.")
        else:
            medals = {0: "🥇", 1: "🥈", 2: "🥉"}
            lb = lb.copy()
            lb["rank"] = [medals.get(i, str(i + 1)) for i in range(len(lb))]
            lb["trader"] = lb.apply(lambda r: r["name"] or _short(r["wallet"]), axis=1)
            lb["profile"] = PROFILE_BASE + lb["wallet"]
            st.dataframe(
                lb[["rank", "trader", "week_pnl", "profile"]],
                width="stretch", hide_index=True,
                column_config={
                    "rank": st.column_config.TextColumn("#", width="small"),
                    "trader": st.column_config.TextColumn("trader"),
                    "week_pnl": st.column_config.NumberColumn("7-day P/L", format="$%.0f"),
                    "profile": st.column_config.LinkColumn("profile", display_text="View ↗"),
                },
            )
            st.caption("P/L is each wallet's total profit change over the last ~7 days "
                       "(across all their Polymarket markets, not just the World Cup).")


# --- Largest Bets (history) ---------------------------------------------
with tab_history:
    st.caption(f"The biggest World Cup match bets recently — single fills ≥ "
               f"${large_bet_usd:,.0f}, newest first. (Recent flow, ~last few days.)")
    hist = trades.fetch_wc_large_prints(min_usd=large_bet_usd)
    if hist.empty:
        st.info("No large WC bets at this threshold — lower it in the sidebar.")
    else:
        st.caption(f"{len(hist)} bets · ${hist['usd'].sum():,.0f} total")
        h = hist.copy()
        h["when"] = _local_dt(h["timestamp"])
        h["trader"] = h.apply(lambda r: r["display_name"] or _short(r["wallet"]), axis=1)
        h["profile"] = PROFILE_BASE + h["wallet"]
        st.dataframe(
            h[["when", "market", "pick", "side", "usd", "trader", "profile"]],
            width="stretch", hide_index=True,
            column_config={
                "when": st.column_config.DatetimeColumn("when", format="MMM D, HH:mm"),
                "market": st.column_config.TextColumn("market", width="large"),
                "pick": st.column_config.TextColumn("pick", width="small"),
                "side": st.column_config.TextColumn("side", width="small"),
                "usd": st.column_config.NumberColumn("amount", format="$%.0f"),
                "trader": st.column_config.TextColumn("trader"),
                "profile": st.column_config.LinkColumn("profile", display_text="View ↗"),
            },
        )
