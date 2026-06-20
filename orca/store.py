"""SQLite persistence for the watchlist and flag history.

Two tables:
  wallets  — the growing roster. first_seen / last_seen let you tell a repeat
             sharp from a stranger; profile fields are refreshed each scan.
  flags    — every (market, outcome) flag we've recorded, with a timestamp,
             so you can see history and "new vs returning".

All functions take an explicit connection so the Streamlit layer controls its
lifecycle. Writes are upserts keyed on wallet / (scan_ts, condition_id, outcome).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable

import pandas as pd

from . import config
from .profiling import WalletProfile
from .triggers import Flag


def connect(path: str | None = None) -> sqlite3.Connection:
    # check_same_thread=False: Streamlit reruns the script on different worker
    # threads but reuses our cached connection, so cross-thread use must be
    # allowed. Safe here because a single-user app serialises its writes.
    conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wallets (
            wallet        TEXT PRIMARY KEY,
            display_name  TEXT,
            first_seen    INTEGER,
            last_seen     INTEGER,
            grade         INTEGER,
            is_mm_hedge   INTEGER,
            value_usd     REAL,
            traded_usd    REAL,
            realized_pnl  REAL,
            win_rate      REAL,
            concentration REAL
        );

        CREATE TABLE IF NOT EXISTS flags (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_ts       INTEGER,
            condition_id  TEXT,
            match_title   TEXT,
            match_time    TEXT,
            grp           TEXT,
            question      TEXT,
            bet_label     TEXT,
            outcome_index INTEGER,
            triggers      TEXT,
            total_usd     REAL,
            n_accounts    INTEGER,
            top_usd       REAL,
            UNIQUE(scan_ts, condition_id, outcome_index)
        );
        """
    )
    conn.commit()


def upsert_wallet(
    conn: sqlite3.Connection,
    profile: WalletProfile,
    display_name: str = "",
    now: int | None = None,
) -> None:
    now = now or int(time.time())
    conn.execute(
        """
        INSERT INTO wallets (
            wallet, display_name, first_seen, last_seen, grade, is_mm_hedge,
            value_usd, traded_usd, realized_pnl, win_rate, concentration
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            display_name  = excluded.display_name,
            last_seen     = excluded.last_seen,
            grade         = excluded.grade,
            is_mm_hedge   = excluded.is_mm_hedge,
            value_usd     = excluded.value_usd,
            traded_usd    = excluded.traded_usd,
            realized_pnl  = excluded.realized_pnl,
            win_rate      = excluded.win_rate,
            concentration = excluded.concentration
        """,
        (
            profile.wallet, display_name, now, now, profile.grade,
            int(profile.is_mm_hedge), profile.value_usd, profile.traded_usd,
            profile.realized_pnl, profile.win_rate, profile.concentration,
        ),
    )


def record_flags(
    conn: sqlite3.Connection,
    flags: Iterable[Flag],
    scan_ts: int | None = None,
) -> None:
    scan_ts = scan_ts or int(time.time())
    for f in flags:
        conn.execute(
            """
            INSERT OR IGNORE INTO flags (
                scan_ts, condition_id, match_title, match_time, grp, question,
                bet_label, outcome_index, triggers, total_usd, n_accounts, top_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_ts, f.condition_id, f.match_title, f.match_time, f.group,
                f.question, f.bet_label, f.outcome_index, "+".join(f.triggers),
                f.total_usd, f.n_accounts, f.top_usd,
            ),
        )
    conn.commit()


def persist_scan(
    conn: sqlite3.Connection,
    flags: list[Flag],
    profiles: dict[str, WalletProfile],
    names: dict[str, str] | None = None,
    scan_ts: int | None = None,
) -> None:
    """Persist a whole scan: flags + every profiled wallet."""
    scan_ts = scan_ts or int(time.time())
    names = names or {}
    record_flags(conn, flags, scan_ts)
    for wallet, profile in profiles.items():
        upsert_wallet(conn, profile, names.get(wallet, ""), scan_ts)
    conn.commit()


def watchlist_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM wallets ORDER BY grade DESC, last_seen DESC", conn
    )


def flags_history_frame(conn: sqlite3.Connection, scan_ts: int | None = None) -> pd.DataFrame:
    if scan_ts is not None:
        return pd.read_sql_query(
            "SELECT * FROM flags WHERE scan_ts = ? ORDER BY total_usd DESC",
            conn, params=(scan_ts,),
        )
    return pd.read_sql_query("SELECT * FROM flags ORDER BY scan_ts DESC, total_usd DESC", conn)
