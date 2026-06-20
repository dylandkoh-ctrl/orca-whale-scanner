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

from orca import config, discovery, profiling, store, trades
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
min_print_usd = st.sidebar.number_input("Live print ≥ $ (Trigger D)",
                                        value=config.LARGE_PRINT_USD, step=50_000)

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

tab_top, tab_prints, tab_flagged, tab_watch, tab_consensus = st.tabs(
    ["🐋 Top Positions", "🔴 Live Prints", "🚩 Flagged Now",
     "📋 Watchlist", "📊 Consensus Board"]
)


def _grade_badge(p) -> str:
    if p is None:
        return "—"
    return f"{p.grade}/100" + (" · ⚠️ MM/HEDGE" if p.is_mm_hedge else "")


_TRIGGER_NAMES = {"A": "single whale", "B": "combined", "C": "consensus (3+)"}


def _triggers_pretty(trigs) -> str:
    if isinstance(trigs, str):
        trigs = trigs.split("+")
    return ", ".join(_TRIGGER_NAMES.get(t, t) for t in trigs)


def _trigger_legend() -> str:
    return (f"**A** one wallet ≥ ${thresholds['single_whale_usd']:,.0f} · "
            f"**B** combined ≥ ${thresholds['combined_usd']:,.0f} · "
            f"**C** 3+ wallets each ≥ ${thresholds['cluster_wallet_usd']:,.0f}")


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


# --- Live Prints (Trigger D) --------------------------------------------
with tab_prints:
    st.caption(f"Single fills ≥ ${min_print_usd:,.0f} on {result.date}'s matches — "
               "live order flow, newest first. Catches the entry before it lands "
               "in holders. (Shows recent flow, ~last few days.)")
    force = st.button("🔄 Check for new prints", width="stretch")
    prints = trades.fetch_large_prints(
        result.markets, min_usd=min_print_usd, ttl=0 if force else config.TRADES_TTL)
    if prints.empty:
        st.info("No large prints at this threshold yet. Lower it in the sidebar, "
                "or check back closer to / during a match.")
    else:
        # Trigger D feeds the watchlist: profile the print wallets and persist.
        pw = [w for w in prints["wallet"].unique() if w]
        pprofiles = profiling.profile_wallets(pw)
        pnames = {r.wallet: r.display_name for r in prints.itertuples() if r.display_name}
        store.persist_scan(conn, [], pprofiles, pnames)

        st.caption(f"{len(prints)} prints · {prints['usd'].sum():,.0f} total $ flagged")
        for r in prints.itertuples():
            ts = dt.datetime.fromtimestamp(r.timestamp).strftime("%b %d · %H:%M")
            dot = "🟢" if r.side == "BUY" else "🔴"
            with st.container(border=True):
                st.markdown(f"{dot} **{r.side} · {r.bet_label}**  ·  ${r.usd:,.0f}")
                c0, c1 = st.columns([3, 2])
                c0.markdown(f"👤 **{r.display_name or r.wallet[:6] + '…' + r.wallet[-4:]}**")
                c0.caption(f"{r.match_title} · {ts} · {int(r.size):,} sh @ ${r.price:.3f}")
                c1.link_button("Polymarket ↗", PROFILE_BASE + r.wallet, width="stretch")


# --- Flagged Now (grouped by match) -------------------------------------
with tab_flagged:
    st.caption("Bets where whale money has concentrated, grouped by match.")
    if not result.flags:
        st.info("No flagged bets at current thresholds. Lower them in the sidebar.")
    else:
        by_match: dict[str, list] = {}
        for f in result.flags:
            by_match.setdefault(f.match_title, []).append(f)

        for match_title in sorted(by_match, key=lambda m: -sum(f.total_usd for f in by_match[m])):
            flags = by_match[match_title]
            tot = sum(f.total_usd for f in flags)
            st.markdown(f"#### ⚽ {match_title}")
            st.caption(f"{len(flags)} flagged bets · ${tot:,.0f} whale money")
            for f in flags:
                best = max((result.profiles.get(w) for w in f.wallets if result.profiles.get(w)),
                           key=lambda p: p.grade, default=None)
                header = (f"**{f.bet_label}** — ${f.total_usd:,.0f} · "
                          f"{f.n_accounts} accounts · grade {_grade_badge(best)}")
                with st.expander(header):
                    st.caption(f"Signal: {_triggers_pretty(f.triggers)} · "
                               f"largest single wallet ${f.top_usd:,.0f}")
                    rows = []
                    for w in f.wallets:
                        p = result.profiles.get(w)
                        rows.append({
                            "account": result.names.get(w) or _short(w),
                            "grade": p.grade if p else None,
                            "realized_pnl": p.realized_pnl if p else None,
                            "portfolio": p.value_usd if p else None,
                            "flag": "⚠️ MM" if (p and p.is_mm_hedge) else "",
                        })
                    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                                 column_config={
                                     "account": st.column_config.TextColumn("account"),
                                     "grade": st.column_config.ProgressColumn(
                                         "grade", min_value=0, max_value=100),
                                     "realized_pnl": st.column_config.NumberColumn("realized PnL", format="$%.0f"),
                                     "portfolio": st.column_config.NumberColumn("portfolio", format="$%.0f"),
                                     "flag": st.column_config.TextColumn(" ", width="small"),
                                 })
                    mkt = next((m for m in result.markets if m["condition_id"] == f.condition_id), None)
                    if mkt and mkt.get("resolution_text"):
                        with st.expander("📋 How this market resolves"):
                            st.write(mkt["resolution_text"])
        st.caption(_trigger_legend())


# --- Watchlist -----------------------------------------------------------
with tab_watch:
    st.caption("Every wallet that's tripped a trigger or printed big — your growing roster of sharps.")
    wl = store.watchlist_frame(conn)
    if wl.empty:
        st.info("Watchlist is empty — it fills as wallets trip a trigger or print big.")
    else:
        # New = only ever seen in one scan; Returning = seen across scans.
        wl["status"] = wl.apply(
            lambda r: "🆕 New" if (r["last_seen"] - r["first_seen"]) <= 60
            else "↩️ Returning", axis=1)
        n_new = int((wl["status"] == "🆕 New").sum())
        n_ret = int((wl["status"] == "↩️ Returning").sum())

        a, b, c = st.columns(3)
        a.metric("Wallets tracked", len(wl))
        b.metric("🆕 New", n_new)
        c.metric("↩️ Returning", n_ret)

        wl["account"] = wl.apply(
            lambda r: r["display_name"] or _short(r["wallet"]), axis=1)
        wl["mm"] = wl["is_mm_hedge"].map({1: "⚠️", 0: ""})
        wl["last_seen_dt"] = pd.to_datetime(wl["last_seen"], unit="s")
        wl = wl.sort_values(["status", "grade"], ascending=[True, False])
        st.dataframe(
            wl[["status", "account", "mm", "grade", "realized_pnl",
                "value_usd", "win_rate", "last_seen_dt"]],
            width="stretch", hide_index=True,
            column_config={
                "status": st.column_config.TextColumn("status", width="small"),
                "mm": st.column_config.TextColumn(" ", width="small"),
                "grade": st.column_config.ProgressColumn("grade", min_value=0, max_value=100),
                "realized_pnl": st.column_config.NumberColumn("realized PnL", format="$%.0f"),
                "value_usd": st.column_config.NumberColumn("portfolio", format="$%.0f"),
                "win_rate": st.column_config.NumberColumn("win rate", format="%.0f%%"),
                "last_seen_dt": st.column_config.DatetimeColumn("last seen", format="MMM D, HH:mm"),
            },
        )
        st.caption("Grades & PnL are from each wallet's current open book (an "
                   "approximation of skill). Note: roster resets when the cloud app reboots.")


# --- Consensus Board -----------------------------------------------------
with tab_consensus:
    st.caption("Where the money agrees — the bets the most whale dollars and the "
               "most *different* whales are piling onto.")
    cons = result.consensus
    if cons.empty:
        st.info("No consensus bets yet at current thresholds.")
    else:
        cons = cons.copy()
        cons["signal"] = cons["triggers"].apply(_triggers_pretty)
        st.dataframe(
            cons[["match_title", "bet_label", "n_accounts", "total_usd", "top_usd", "signal"]],
            width="stretch", hide_index=True,
            column_config={
                "match_title": st.column_config.TextColumn("match"),
                "bet_label": st.column_config.TextColumn("bet"),
                "n_accounts": st.column_config.NumberColumn("# whales"),
                "total_usd": st.column_config.NumberColumn("combined $", format="$%.0f"),
                "top_usd": st.column_config.NumberColumn("largest wallet", format="$%.0f"),
                "signal": st.column_config.TextColumn("signal"),
            },
        )
        st.caption(_trigger_legend())
