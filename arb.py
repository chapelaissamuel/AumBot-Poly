"""
Arbitrage detector — scans binary YES/NO Polymarket markets.
Real arbitrage: yes_price + no_price < 0.98 on the SAME market.
Multi-outcome markets are explicitly excluded.
"""
import json
import logging
import requests

logger = logging.getLogger(__name__)

MARKETS_URL = "https://gamma-api.polymarket.com/markets"
TIMEOUT = 15
ARB_MAX_SUM = 0.98   # sum(yes + no) must be BELOW this to flag arbitrage


def _parse_binary_prices(market: dict) -> tuple[float, float] | None:
    """
    Return (yes_price, no_price) only for binary YES/NO markets (exactly 2 outcomes).
    Returns None for multi-outcome markets or parse errors.
    """
    raw = market.get("outcomePrices", '["0.5","0.5"]')
    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except Exception:
            return None
    else:
        prices = raw if isinstance(raw, list) else []

    # Strict binary check — must have exactly 2 outcomes
    if len(prices) != 2:
        return None

    try:
        yes_price = float(prices[0])
        no_price = float(prices[1])
    except Exception:
        return None

    # Skip dead / resolved / degenerate prices
    if yes_price <= 0 or no_price <= 0 or yes_price >= 1 or no_price >= 1:
        return None

    return yes_price, no_price


def fetch_arb_opportunities(limit: int = 500) -> list[dict]:
    """
    Scan active binary Polymarket markets.
    Arbitrage exists when yes_price + no_price < 0.98 on the SAME market:
    buying both YES and NO costs < $0.98 for a guaranteed $1.00 payout.

    Returns list sorted by gap (largest first), each entry contains:
        question, yes_price, no_price, sum_prices, gap_cents, volume, url
    """
    try:
        resp = requests.get(
            MARKETS_URL,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
                "limit": limit,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("[ARB] fetch failed: %s", exc)
        return []

    # Gamma API may return a list directly or {"markets": [...]}
    if isinstance(data, list):
        markets = data
    elif isinstance(data, dict):
        markets = data.get("markets", [])
    else:
        return []

    opportunities = []

    for m in markets:
        if not isinstance(m, dict):
            continue

        parsed = _parse_binary_prices(m)
        if parsed is None:
            continue

        yes_price, no_price = parsed
        sum_prices = yes_price + no_price

        # Only flag when buying both sides is profitable
        if sum_prices >= ARB_MAX_SUM:
            continue

        gap_cents = round((1.0 - sum_prices) * 100, 2)

        opportunities.append({
            "question":   (m.get("question") or "Unknown")[:100],
            "yes_price":  yes_price,
            "no_price":   no_price,
            "sum_prices": round(sum_prices, 4),
            "gap_cents":  gap_cents,
            "volume":     float(m.get("volume", 0) or 0),
            "slug":       m.get("slug", ""),
            "url":        f"https://polymarket.com/event/{m.get('slug', '')}",
        })

    opportunities.sort(key=lambda x: x["gap_cents"], reverse=True)
    logger.info("[ARB] scanned %d markets → %d binary arb opportunities", len(markets), len(opportunities))
    return opportunities
