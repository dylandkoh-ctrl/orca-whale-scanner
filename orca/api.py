"""Thin HTTP layer for the three Polymarket API surfaces.

Responsibilities:
  * one shared httpx client,
  * exponential backoff on rate limits / transient errors,
  * a tiny in-process TTL cache so a refresh doesn't hammer the same endpoint,
  * helpers for the JSON-encoded-string fields Gamma returns.

Verified field quirks this layer protects you from:
  * Gamma `clobTokenIds`, `outcomes`, `outcomePrices` arrive as JSON *strings*.
  * CLOB `/price` and `/midpoint` return prices as strings.
  * `/holders` is grouped by token, not a flat list (handled in holders.py).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, TypeVar

import httpx

from . import config

T = TypeVar("T")
R = TypeVar("R")

# --- shared client + naive TTL cache -------------------------------------
_client = httpx.Client(
    timeout=config.HTTP_TIMEOUT,
    headers={"User-Agent": "orca-whale-scanner/0.1"},
)
_cache: dict[str, tuple[float, Any]] = {}


def _cache_key(host: str, path: str, params: dict | None) -> str:
    return f"{host}{path}?{json.dumps(params or {}, sort_keys=True)}"


def get_json(
    host: str,
    path: str,
    params: dict | None = None,
    ttl: float = 0,
) -> Any:
    """GET `host+path` and return parsed JSON, with backoff and optional caching.

    `ttl` of 0 disables caching for this call.
    """
    key = _cache_key(host, path, params)
    if ttl > 0:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]

    url = f"{host}{path}"
    backoff = 1.0
    last_exc: Exception | None = None
    for attempt in range(config.HTTP_MAX_RETRIES):
        try:
            resp = _client.get(url, params=params)
            # Retry on rate limit / server errors; raise on other 4xx.
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"{resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            data = resp.json()
            if ttl > 0:
                _cache[key] = (time.time(), data)
            return data
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < config.HTTP_MAX_RETRIES - 1:
                time.sleep(backoff)
                backoff *= 2  # exponential
    raise RuntimeError(f"GET {url} failed after retries: {last_exc}")


def parallel_map(fn: Callable[[T], R], items: Iterable[T],
                 workers: int = config.HTTP_MAX_WORKERS) -> list[R]:
    """Run `fn` over `items` concurrently, preserving order.

    httpx.Client is thread-safe, so this just fans out the (I/O-bound) HTTP
    calls. Used to parallelise the per-market holder pulls and per-wallet
    profiling that otherwise dominate scan time.
    """
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


# --- helpers for Gamma's stringified JSON fields -------------------------
def parse_json_field(value: Any) -> Any:
    """Gamma encodes arrays as JSON strings. Decode safely; pass through lists."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def to_float(value: Any, default: float = 0.0) -> float:
    """Cast a maybe-string price/amount to float without exploding on junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
