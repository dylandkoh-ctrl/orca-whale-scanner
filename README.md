# Orca — World Cup Whale Scanner

Finds large Polymarket bets on **single World Cup matches for a given day** —
moneyline ("USA Win: No"), totals ("O/U 2.5: Over"), both-teams-to-score,
spreads — grades the wallets behind them, and keeps a growing SQLite watchlist.
Pick the match day in the sidebar.

## Run

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m streamlit run app.py
```

Pick the match day in the sidebar; click **Refresh scan**.

## Tabs

- **🐋 Top Positions** — the largest single-match bets for the chosen day, entry
  cost vs. current value + PnL, with the wallet behind each.
- **🏆 Weekly Leaderboard** — 7-day P/L of the whales making large WC bets.
- **📜 Largest Bets** — recent history of the biggest WC match bets and who made them.

## Layout

| File | Role |
|---|---|
| `orca/config.py` | Hosts, cache TTLs, WC tag, market-family toggles, thresholds |
| `orca/api.py` | httpx client: backoff, TTL cache, parallel_map, decodes stringified JSON |
| `orca/discovery.py` | Gamma WC-tag → a day's match markets (viewer-tz aware) |
| `orca/holders.py` | `/holders` → tidy `wallet × bet × USD` DataFrame |
| `orca/profiling.py` | `/positions` + `/value` → entry/current detail, MM filter, grade |
| `orca/trades.py` | `/trades` CASH-filtered large prints (per-day and WC-wide) |
| `orca/leaderboard.py` | `user-pnl` per-wallet 7-day P/L → weekly leaderboard |
| `orca/triggers.py` | Triggers A/B/C (used internally to pick wallets to profile) |
| `orca/store.py` | SQLite persistence |
| `orca/scan.py` | One refresh = one scan; ties the pipeline together |
| `app.py` | Streamlit UI — Top Positions / Weekly Leaderboard / Largest Bets |

## Notes baked in from live-API verification

- Gamma `clobTokenIds` / `outcomes` / `outcomePrices` are JSON **strings** — decoded in `api.py`.
- `/holders` is grouped **by token**, `amount` is in **shares** — priced to USD from Gamma.
- Match markets use `["Yes","No"]` (moneyline/BTTS) or `["Over","Under"]` (totals);
  **both sides are directional**, so a bet is one side of one market.
- Discovery pages the **FIFA World Cup tag (102232)** and filters to the day's
  `Team vs. Team` events (rejects prop events like "What will the announcers say…").
- Thresholds are calibrated to single-match scale (moneyline whales $400K–$1M;
  totals top ~$50–90K) and editable live in the sidebar.
- No public global leaderboard endpoint, but `user-pnl-api.../user-pnl?interval=1w`
  returns a per-wallet cumulative P/L series — 7-day P/L = last point − first point.
- WC match trades are spotted in the global `/trades` feed by `eventSlug` prefix `fifwc-`.
