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

Click **Refresh scan** in the sidebar. Thresholds (triggers A/B/C) are editable there.

## Layout

| File | Role |
|---|---|
| `orca/config.py` | Thresholds, hosts, cache TTLs, WC tag, market-family toggles |
| `orca/api.py` | httpx client: backoff, TTL cache, decodes Gamma's stringified JSON |
| `orca/discovery.py` | Gamma WC-tag → that day's match markets (moneyline/totals/...) |
| `orca/holders.py` | `/holders` → tidy `wallet × bet × USD` DataFrame |
| `orca/triggers.py` | Triggers A/B/C over the holders table |
| `orca/profiling.py` | `/positions` + `/value` + `/traded` → PnL, win rate, MM filter, 0–100 grade |
| `orca/store.py` | SQLite watchlist + flag history (`first_seen` / `last_seen`) |
| `orca/scan.py` | One refresh = one scan; ties the pipeline together |
| `app.py` | Streamlit UI — Flagged Now / Watchlist / Consensus Board |

## v1 scope

That day's match markets (Moneyline + More Markets by default; Halftime, Exact
Score, Corners, Player Props are one toggle away in `config.MARKET_GROUPS`) +
holders + triggers A/B/C + watchlist. **Held for later:** Trigger D (`/trades`
polling), APScheduler cron, CLOB WebSocket.

## Notes baked in from live-API verification

- Gamma `clobTokenIds` / `outcomes` / `outcomePrices` are JSON **strings** — decoded in `api.py`.
- `/holders` is grouped **by token**, `amount` is in **shares** — priced to USD from Gamma.
- Match markets use `["Yes","No"]` (moneyline/BTTS) or `["Over","Under"]` (totals);
  **both sides are directional**, so a bet is one side of one market.
- Discovery pages the **FIFA World Cup tag (102232)** and filters to the day's
  `Team vs. Team` events (rejects prop events like "What will the announcers say…").
- Thresholds are calibrated to single-match scale (moneyline whales $400K–$1M;
  totals top ~$50–90K) and editable live in the sidebar.
- The dedicated **leaderboard endpoint 404s**; grading uses `/positions` + `/value` + `/traded`.
