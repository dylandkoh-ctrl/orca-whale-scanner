"""
Orca — Wallet Watchlist & Tail Alerts (World Cup scope)
=======================================================

Polls a curated list of "smart money" wallets, filters their Polymarket
activity down to World Cup markets + a size threshold, and pushes a formatted
alert to Telegram. Runs as a standalone background worker ALONGSIDE the
Streamlit scanner — it imports Orca's API client but doesn't touch the app.

Run:  python wallet_watchlist.py   (from the repo root)

Integration notes:
  * Polymarket calls go through Orca's shared client (orca.api.get_json) — no
    duplicate HTTP/backoff/parse code here.
  * Field names below are VERIFIED against live /activity responses:
      timestamp, side, size, price, usdcSize, conditionId, asset, outcome,
      title, eventSlug, transactionHash, type.
  * The watchlist is SEEDED with real wallet addresses from Orca's WC large-bet
    data (usernames don't work as /activity?user=).
  * Telegram secrets live in a gitignored .env (see .env.example).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

import httpx
from dotenv import load_dotenv

from orca import config, discovery, leaderboard, trades
from orca.api import get_json, to_float

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
load_dotenv()  # pulls TELEGRAM_* from a gitignored .env
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL = 60             # seconds between full cycles
MIN_NOTIONAL_USD = 300_000    # tail threshold — only alert on whale-sized fills
ALERT_SIDES = {"BUY"}        # tail directional entries; add "SELL" if wanted
DB_PATH = "orca_watchlist.db"

SEED_TOP_N = 20                       # how many leaderboard wallets to tail
SEED_MIN_BET_USD = config.LARGE_PRINT_USD  # universe = wallets making >= this WC bet
RESEED_EVERY = 30                     # re-seed the watchlist every N cycles

# Optional manual additions — ADDRESSES ONLY (0x…), not usernames.
MANUAL_WATCHLIST: list[str] = []

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("orca.watchlist")


# --------------------------------------------------------------------------
# SEEDING — reuse Orca's WC market fetch + leaderboard (no placeholders)
# --------------------------------------------------------------------------
def build_wc_condition_ids() -> set[str]:
    """All conditionIds for current + upcoming WC matches, via Orca discovery."""
    cids: set[str] = set()
    for day in discovery.list_match_days(limit_days=7):
        for m in discovery.discover_matches(date=day):
            if m.get("condition_id"):
                cids.add(m["condition_id"])
    return cids


def seed_watchlist(top_n: int = SEED_TOP_N
                   ) -> tuple[list[str], dict[str, str], dict[str, dict]]:
    """Top-N wallet ADDRESSES from Orca's WC large-bettor leaderboard.

    Universe = wallets making large (>= SEED_MIN_BET_USD) WC bets; ranked by
    7-day P/L scaled by a sample-size-shrunk realized win rate
    (leaderboard.smart_money_leaderboard) so hot streaks on thin records don't
    dominate.

    Returns (addresses, name_map, stats) where stats[wallet] =
    {win_rate, resolved_n, week_pnl} — reused in the alert so we don't recompute
    the win rate on every fill.
    """
    prints = trades.fetch_wc_large_prints(min_usd=SEED_MIN_BET_USD)
    if prints.empty:
        return list(MANUAL_WATCHLIST), {}, {}
    names = {r.wallet: r.display_name for r in prints.itertuples() if r.display_name}
    lb = leaderboard.smart_money_leaderboard(prints["wallet"].tolist(), names)
    top = lb.head(top_n)
    addresses = list(dict.fromkeys(top["wallet"].tolist() + MANUAL_WATCHLIST))
    name_map = {r["wallet"]: (r["name"] or r["wallet"][:8]) for _, r in top.iterrows()}
    stats = {r["wallet"]: {"win_rate": r["win_rate"], "resolved_n": int(r["resolved_n"]),
                           "week_pnl": r["week_pnl"]} for _, r in top.iterrows()}
    return addresses, name_map, stats


# --------------------------------------------------------------------------
# STATE (restart-safe: last-seen cursor per wallet + alert dedupe)
# --------------------------------------------------------------------------
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS wallet_cursor (
        wallet TEXT PRIMARY KEY, last_seen_ts INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alerted (
        key TEXT PRIMARY KEY, ts INTEGER NOT NULL)""")
    conn.commit()
    return conn


def get_cursor(conn: sqlite3.Connection, wallet: str) -> int:
    row = conn.execute(
        "SELECT last_seen_ts FROM wallet_cursor WHERE wallet = ?", (wallet,)).fetchone()
    return row[0] if row else 0


def set_cursor(conn: sqlite3.Connection, wallet: str, ts: int) -> None:
    conn.execute(
        "INSERT INTO wallet_cursor(wallet, last_seen_ts) VALUES(?, ?) "
        "ON CONFLICT(wallet) DO UPDATE SET last_seen_ts = excluded.last_seen_ts",
        (wallet, ts))
    conn.commit()


def already_alerted(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM alerted WHERE key = ?", (key,)).fetchone() is not None


def mark_alerted(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("INSERT OR IGNORE INTO alerted(key, ts) VALUES(?, ?)",
                 (key, int(time.time())))
    conn.commit()


# --------------------------------------------------------------------------
# POLYMARKET FETCH (via Orca's client)
# --------------------------------------------------------------------------
def fetch_activity(wallet: str, since_ts: int) -> list[dict]:
    """TRADE activity for one wallet, newest first, strictly newer than cursor.

    type=TRADE is filtered server-side (verified); the feed is already DESC.
    ttl=0 so the tail always sees fresh fills.
    """
    try:
        items = get_json(config.DATA_HOST, "/activity",
                         params={"user": wallet, "limit": 100, "type": "TRADE"},
                         ttl=0) or []
    except RuntimeError as e:
        log.warning("activity fetch failed for %s: %s", wallet[:10], e)
        return []
    return [it for it in items if int(it.get("timestamp", 0)) > since_ts]


def current_price(token_id: str) -> float | None:
    """Midpoint for the outcome token (entry-vs-now context). /midpoint -> {"mid": "0.x"}."""
    if not token_id:
        return None
    try:
        data = get_json(config.CLOB_HOST, "/midpoint", params={"token_id": token_id}, ttl=15)
    except RuntimeError:
        return None
    mid = to_float(data.get("mid")) if isinstance(data, dict) else 0.0
    return mid or None


# --------------------------------------------------------------------------
# DETECTION
# --------------------------------------------------------------------------
def notional_usd(item: dict) -> float:
    """USD notional — /activity carries it directly as usdcSize; fall back to size*price."""
    if item.get("usdcSize") is not None:
        return to_float(item["usdcSize"])
    return to_float(item.get("size")) * to_float(item.get("price"))


def is_world_cup(item: dict, wc_condition_ids: set[str]) -> bool:
    """A WC match bet: conditionId in the fetched set, or a fifwc- event slug."""
    if item.get("conditionId") in wc_condition_ids:
        return True
    return (item.get("eventSlug") or "").startswith(config.WC_MATCH_SLUG_PREFIX)


def qualifies(item: dict, wc_condition_ids: set[str]) -> bool:
    if item.get("side") not in ALERT_SIDES:
        return False
    if not is_world_cup(item, wc_condition_ids):
        return False
    return notional_usd(item) >= MIN_NOTIONAL_USD


def dedupe_key(wallet: str, item: dict) -> str:
    return f"{wallet}:{item.get('transactionHash','')}:{item.get('asset','')}:{item.get('side','')}"


# --------------------------------------------------------------------------
# TELEGRAM
# --------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")
        return
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("telegram send failed: %s", e)


def format_alert(wallet: str, item: dict, names: dict[str, str],
                 stats: dict[str, dict]) -> str:
    entry = to_float(item.get("price"))
    notion = notional_usd(item)
    title = item.get("title", "Unknown market")
    outcome = item.get("outcome", "?")
    side = item.get("side", "?")
    who = names.get(wallet) or f"{wallet[:6]}…{wallet[-4:]}"

    now = current_price(item.get("asset", ""))
    if now is not None:
        price_line = f"Entry {entry:.3f} → now {now:.3f}  ({(now - entry) * 100:+.1f}¢)"
    else:
        price_line = f"Entry {entry:.3f}"

    # Recent win rate (from seed-time realized record), if we have a sample.
    rec = stats.get(wallet) or {}
    if rec.get("resolved_n"):
        record_line = (f"📊 Win rate {rec['win_rate'] * 100:.0f}% "
                       f"({rec['resolved_n']} resolved) · 7d P/L ${rec['week_pnl']:,.0f}\n")
    else:
        record_line = ""

    return (f"🐋 <b>{side} ${notion:,.0f}</b>\n"
            f"{title} — <b>{outcome}</b>\n"
            f"{price_line}\n"
            f"{record_line}"
            f"{who}  ·  <a href=\"https://polymarket.com/profile/{wallet}\">profile</a>")


# --------------------------------------------------------------------------
# MAIN LOOP
# --------------------------------------------------------------------------
def run() -> None:
    conn = init_db()
    wc_condition_ids = build_wc_condition_ids()
    watchlist, names, stats = seed_watchlist()
    log.info("Orca watchlist started — %d wallets tailed, %d WC condition ids, $%d threshold",
             len(watchlist), len(wc_condition_ids), MIN_NOTIONAL_USD)
    if not watchlist:
        log.warning("watchlist is empty — no large WC bettors found to seed from")

    cycle = 0
    while True:
        for wallet in watchlist:
            cursor = get_cursor(conn, wallet)
            items = fetch_activity(wallet, cursor)
            if not items:
                continue
            newest_ts = cursor
            for item in items:
                newest_ts = max(newest_ts, int(item.get("timestamp", 0)))
                if not qualifies(item, wc_condition_ids):
                    continue
                key = dedupe_key(wallet, item)
                if already_alerted(conn, key):
                    continue
                send_telegram(format_alert(wallet, item, names, stats))
                mark_alerted(conn, key)
                log.info("alerted %s  $%.0f  %s",
                         names.get(wallet, wallet[:8]), notional_usd(item),
                         item.get("title", "")[:40])
            set_cursor(conn, wallet, newest_ts)

        cycle += 1
        if cycle % RESEED_EVERY == 0:
            wc_condition_ids = build_wc_condition_ids()
            watchlist, names, stats = seed_watchlist()
            log.info("re-seeded — %d wallets, %d WC condition ids",
                     len(watchlist), len(wc_condition_ids))
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
