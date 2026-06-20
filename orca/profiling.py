"""Profile a flagged wallet: PnL, win rate, concentration, MM/hedge flag, grade.

Data sources (the dedicated leaderboard endpoint 404s, so we build the picture
from per-wallet endpoints instead):
  * /positions?user=  -> the wallet's open book (per-position realized/unrealized PnL)
  * /value?user=      -> current portfolio value (USD)
  * /traded?user=     -> lifetime traded volume

Caveat baked in: /positions reflects the *current open book*, not full lifetime
closed-trade history, so "win rate" here is win rate over currently-held
positions with a realized component — an approximation, labelled as such.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config
from .api import get_json, parallel_map, to_float


@dataclass
class WalletProfile:
    wallet: str
    value_usd: float          # current portfolio value
    traded_usd: float         # lifetime volume
    realized_pnl: float       # summed across open book
    cash_pnl: float           # realized + unrealized across open book
    n_positions: int
    win_rate: float           # share of positions in profit (approx, see module docstring)
    concentration: float      # 0-1; top single position's share of gross PnL
    is_mm_hedge: bool         # holds both sides of a market, or high volume / ~0 PnL
    grade: int                # 0-100 smart-money grade


def _fetch(wallet: str) -> tuple[list[dict], float, float]:
    positions = get_json(
        config.DATA_HOST, "/positions",
        params={"user": wallet, "sizeThreshold": 1},
        ttl=config.PROFILE_TTL,
    ) or []
    value_resp = get_json(config.DATA_HOST, "/value",
                          params={"user": wallet}, ttl=config.PROFILE_TTL) or []
    traded_resp = get_json(config.DATA_HOST, "/traded",
                           params={"user": wallet}, ttl=config.PROFILE_TTL) or {}

    value = to_float(value_resp[0].get("value")) if value_resp else 0.0
    traded = to_float(traded_resp.get("traded"))
    return positions, value, traded


def _detect_mm_hedge(positions: list[dict], traded: float, cash_pnl: float) -> bool:
    """Two-sided in any single market, or big volume with near-zero PnL."""
    sides_per_market: dict[str, set[int]] = {}
    for p in positions:
        cid = p.get("conditionId")
        sides_per_market.setdefault(cid, set()).add(p.get("outcomeIndex"))
    holds_both = any(len(sides) >= 2 for sides in sides_per_market.values())

    # "High volume with near-zero PnL" — churns size but doesn't make money.
    churn = traded > 500_000 and abs(cash_pnl) < 0.01 * traded
    return holds_both or churn


def _grade(value: float, realized_pnl: float, win_rate: float,
           concentration: float, is_mm_hedge: bool) -> int:
    """Transparent 0-100 heuristic. Tune the weights freely.

    Rewards: real realized profit, a high win rate, and *broad* skill (low
    concentration). Penalises MM/hedge wallets hard so two-sided books don't
    read as directional conviction.
    """
    # Realized PnL component (0-40): saturates at +$250k.
    pnl_score = max(0.0, min(realized_pnl / 250_000, 1.0)) * 40
    # Win-rate component (0-25).
    win_score = win_rate * 25
    # Breadth component (0-20): low concentration == broad, repeatable edge.
    breadth_score = (1.0 - concentration) * 20
    # Size/skin-in-the-game component (0-15): saturates at $1M portfolio.
    size_score = max(0.0, min(value / 1_000_000, 1.0)) * 15

    grade = pnl_score + win_score + breadth_score + size_score
    if is_mm_hedge:
        grade *= 0.4  # down-rank: not a directional signal
    return int(round(max(0.0, min(grade, 100.0))))


def profile_wallet(wallet: str) -> WalletProfile:
    positions, value, traded = _fetch(wallet)

    realized = sum(to_float(p.get("realizedPnl")) for p in positions)
    cash = sum(to_float(p.get("cashPnl")) for p in positions)
    n = len(positions)

    wins = sum(1 for p in positions if to_float(p.get("cashPnl")) > 0)
    win_rate = (wins / n) if n else 0.0

    # Concentration: largest single |PnL| as a share of total gross |PnL|.
    gross = sum(abs(to_float(p.get("cashPnl"))) for p in positions)
    top = max((abs(to_float(p.get("cashPnl"))) for p in positions), default=0.0)
    concentration = (top / gross) if gross > 0 else 0.0

    is_mm = _detect_mm_hedge(positions, traded, cash)
    grade = _grade(value, realized, win_rate, concentration, is_mm)

    return WalletProfile(
        wallet=wallet, value_usd=value, traded_usd=traded,
        realized_pnl=realized, cash_pnl=cash, n_positions=n,
        win_rate=win_rate, concentration=concentration,
        is_mm_hedge=is_mm, grade=grade,
    )


def profile_wallets(wallets: list[str]) -> dict[str, WalletProfile]:
    """Profile a set of wallets, de-duplicated, concurrently."""
    uniq = [w for w in dict.fromkeys(wallets) if w]
    profiles = parallel_map(profile_wallet, uniq)
    return {p.wallet: p for p in profiles}
