"""
kalshi.py — Cross-platform arbitrage: Polymarket vs Kalshi.

Fetches open Kalshi markets, matches them to Polymarket markets by keyword,
detects price divergences > 5 percentage points.
No auth required for public read endpoints.
"""

import logging
import requests
from polymarket_maicr import fetch_markets

logger = logging.getLogger(__name__)
TIMEOUT = 10

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API   = "https://gamma-api.polymarket.com/markets"


# ─── Kalshi fetcher ───────────────────────────────────────────────────────────

def _get_kalshi_markets(limit: int = 100) -> list[dict]:
    """Fetch open Kalshi markets. Returns list of dicts with yes_price."""
    try:
        r = requests.get(
            f"{KALSHI_BASE}/markets",
            params={"limit": limit, "status": "open"},
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if not r.ok:
            logger.warning("[KALSHI] HTTP %s — %s", r.status_code, r.text[:100])
            return []
        data = r.json()
        markets = data.get("markets", [])
        result = []
        for m in markets:
            # Kalshi uses yes_bid / yes_ask; use mid
            yes_bid = float(m.get("yes_bid", 0) or 0)
            yes_ask = float(m.get("yes_ask", 100) or 100)
            yes_price = (yes_bid + yes_ask) / 200.0  # convert cents to 0-1
            title = m.get("title", m.get("question", ""))
            ticker = m.get("ticker", "")
            result.append({
                "title": title,
                "ticker": ticker,
                "yes_price": yes_price,
                "url": f"https://kalshi.com/markets/{ticker}",
                "keywords": title.lower().split(),
            })
        logger.info("[KALSHI] fetched %d open markets", len(result))
        return result
    except Exception as exc:
        logger.warning("[KALSHI] fetch error: %s", exc)
        return []


def _get_polymarket_prices(limit: int = 100) -> list[dict]:
    """Fetch top Polymarket markets with YES prices."""
    try:
        r = requests.get(
            GAMMA_API,
            params={"active": "true", "order": "volume", "limit": limit},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return []
        import json
        markets = r.json() if isinstance(r.json(), list) else []
        result = []
        for m in markets:
            q = m.get("question", "")
            prices_raw = m.get("outcomePrices", '["0.5","0.5"]')
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except Exception:
                    prices = ["0.5", "0.5"]
            else:
                prices = prices_raw
            yes_price = float(prices[0]) if prices else 0.5
            result.append({
                "question": q,
                "yes_price": yes_price,
                "url": m.get("url", ""),
                "keywords": q.lower().split(),
            })
        return result
    except Exception as exc:
        logger.warning("[XARB] polymarket fetch error: %s", exc)
        return []


# ─── Keyword matching ─────────────────────────────────────────────────────────

_STOPWORDS = {
    "will", "the", "a", "an", "be", "in", "to", "of", "for",
    "on", "at", "by", "is", "are", "was", "were", "that", "this",
    "or", "and", "with", "from", "have", "has", "not", "any",
}

def _match_score(kalshi_kws: list[str], poly_kws: list[str]) -> int:
    """Count meaningful keyword overlaps between two market titles."""
    k_set = {w for w in kalshi_kws if len(w) > 3 and w not in _STOPWORDS}
    p_set = {w for w in poly_kws  if len(w) > 3 and w not in _STOPWORDS}
    return len(k_set & p_set)


# ─── Main scanner ─────────────────────────────────────────────────────────────

def find_cross_arb(min_diff_pts: float = 5.0) -> list[dict]:
    """
    Match Kalshi vs Polymarket markets by keyword similarity.
    Return pairs where abs(price_diff) > min_diff_pts percentage points.
    Sorted by divergence descending.
    """
    kalshi_mkts = _get_kalshi_markets()
    poly_mkts   = _get_polymarket_prices()

    if not kalshi_mkts or not poly_mkts:
        logger.warning("[XARB] empty data — kalshi=%d poly=%d", len(kalshi_mkts), len(poly_mkts))
        return []

    divergences = []
    for km in kalshi_mkts:
        best_score = 0
        best_poly = None
        for pm in poly_mkts:
            sc = _match_score(km["keywords"], pm["keywords"])
            if sc > best_score:
                best_score = sc
                best_poly = pm

        if best_score < 2 or best_poly is None:
            continue

        diff = (km["yes_price"] - best_poly["yes_price"]) * 100
        if abs(diff) >= min_diff_pts:
            divergences.append({
                "kalshi_title":  km["title"][:70],
                "kalshi_price":  km["yes_price"],
                "kalshi_url":    km["url"],
                "poly_question": best_poly["question"][:70],
                "poly_price":    best_poly["yes_price"],
                "poly_url":      best_poly["url"],
                "diff_pts":      round(diff, 1),
                "match_score":   best_score,
                "action": (
                    f"BUY Kalshi YES + BUY Poly NO"
                    if diff > 0
                    else f"BUY Poly YES + BUY Kalshi NO"
                ),
            })

    divergences.sort(key=lambda x: abs(x["diff_pts"]), reverse=True)
    logger.info("[XARB] found %d divergences ≥%.0fpts", len(divergences), min_diff_pts)
    return divergences[:10]
