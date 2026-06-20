"""Discover single-match World Cup markets for a given day, via Gamma.

We page through the FIFA World Cup tag, keep events that kick off on the target
date, classify each into a market family (Moneyline / More Markets / ...), and
normalise every market into a tidy dict. Each market carries its match, the
human bet label, the CLOB token ids, and current per-outcome prices (float).

Bet label examples:
  "Will United States win on 2026-06-19?" / outcome "No"  -> "United States: No"
  "... : O/U 2.5" / outcome "Over"                        -> "O/U 2.5: Over"
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from . import config
from .api import get_json, parse_json_field, to_float

# A real single match reads "Team A vs. Team B" — used to reject prop events like
# "What will the announcers say during Mexico vs South Korea ...".
_MATCH_RE = re.compile(r"^.+\svs\.\s.+$")


def _tz(tz: str | None):
    """Resolve a tz name to a tzinfo; None -> the machine's local zone.

    On Streamlit Cloud the server runs in UTC, so callers pass the *browser's*
    timezone to keep "today's games" correct for the user, not the server.
    """
    if tz:
        try:
            return ZoneInfo(tz)
        except Exception:
            return None
    return None


def _today_iso(tz: str | None = None) -> str:
    return dt.datetime.now(_tz(tz)).date().isoformat()


def _local_date(iso_utc: str, tz: str | None = None) -> str:
    """UTC ISO timestamp -> calendar date (ISO) in the given timezone.

    Match endDates are UTC, but "today's games" means today in the *viewer's*
    timezone — an evening US kickoff on the 19th is already the 20th in UTC, so
    comparing raw UTC date strings drops those matches.
    """
    try:
        return dtparser.isoparse(iso_utc).astimezone(_tz(tz)).date().isoformat()
    except (ValueError, TypeError):
        return (iso_utc or "")[:10]


def _is_match(title: str) -> bool:
    return bool(_MATCH_RE.match(title.strip()))


def _classify_group(title: str) -> tuple[str, str]:
    """Split an event title into (match_title, group).

    "United States vs. Australia - More Markets" -> ("United States vs. Australia", "More Markets")
    "United States vs. Australia"                 -> ("United States vs. Australia", "Moneyline")
    """
    if " - " in title:
        match_title, suffix = title.rsplit(" - ", 1)
        return match_title.strip(), suffix.strip()
    return title.strip(), "Moneyline"


def _bet_label(group_item_title: str, outcome: str, question: str) -> str:
    """Human-readable bet, e.g. 'United States: No' or 'O/U 2.5: Over'."""
    base = (group_item_title or question or "").strip()
    # "Draw (United States vs. Australia)" -> "Draw"
    if base.startswith("Draw"):
        base = "Draw"
    return f"{base}: {outcome}"


def _normalise_market(m: dict[str, Any], match_title: str, group: str,
                      match_time: str) -> dict[str, Any] | None:
    token_ids = parse_json_field(m.get("clobTokenIds")) or []
    outcomes = parse_json_field(m.get("outcomes")) or []
    prices = [to_float(p) for p in (parse_json_field(m.get("outcomePrices")) or [])]
    if not token_ids or len(token_ids) != len(prices) or m.get("closed"):
        return None

    git = m.get("groupItemTitle") or ""
    return {
        "condition_id": m.get("conditionId"),
        "match_title": match_title,        # "United States vs. Australia"
        "match_time": match_time,          # ISO kickoff/end datetime
        "group": group,                    # "Moneyline" / "More Markets" / ...
        "question": m.get("question"),
        "bet_group": git,                  # e.g. "United States", "O/U 2.5"
        "slug": m.get("slug"),
        "outcomes": outcomes,              # e.g. ["Yes","No"] or ["Over","Under"]
        "token_ids": token_ids,
        "prices": prices,
        # per-outcome bet labels, parallel to outcomes
        "bet_labels": [_bet_label(git, o, m.get("question", "")) for o in outcomes],
        "resolution_text": (m.get("description") or "").strip(),
    }


def _iter_wc_events() -> list[dict[str, Any]]:
    """Page through all open World Cup events via the tag."""
    events: list[dict[str, Any]] = []
    offset = 0
    while offset < config.DISCOVERY_MAX_EVENTS:
        page = get_json(
            config.GAMMA_HOST, "/events",
            params={"tag_id": config.WC_TAG_ID, "closed": "false",
                    "limit": 100, "offset": offset},
            ttl=config.DISCOVERY_TTL,
        ) or []
        events.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return events


def discover_matches(date: str | None = None,
                     groups: dict[str, bool] | None = None,
                     tz: str | None = None) -> list[dict[str, Any]]:
    """Normalised markets for all matches kicking off on `date` (default today).

    `tz` is the viewer's timezone (IANA name); dates are computed in it.
    Only the market families enabled in `groups` (default config.MARKET_GROUPS)
    are returned.
    """
    date = date or _today_iso(tz)
    groups = groups or config.MARKET_GROUPS

    markets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in _iter_wc_events():
        end = event.get("endDate") or ""
        if _local_date(end, tz) != date:
            continue
        match_title, group = _classify_group(event.get("title", ""))
        if not _is_match(match_title) or not groups.get(group, False):
            continue
        for raw in event.get("markets", []):
            norm = _normalise_market(raw, match_title, group, end)
            if not norm:
                continue
            cid = norm["condition_id"]
            if not cid or cid in seen:
                continue
            seen.add(cid)
            markets.append(norm)
    return markets


def list_match_days(limit_days: int = 7, tz: str | None = None) -> list[str]:
    """Distinct upcoming match dates (ISO, in `tz`) found in the WC tag, sorted."""
    days: set[str] = set()
    for event in _iter_wc_events():
        end = event.get("endDate")
        match_title, _ = _classify_group(event.get("title", ""))
        if end and _is_match(match_title):
            days.add(_local_date(end, tz))
    return sorted(days)[:limit_days]
