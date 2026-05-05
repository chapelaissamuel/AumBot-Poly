"""
Arbitrage detector — scans Polymarket for intra-event sum(YES) > 1.02.
"""
import json
import logging
import requests

logger = logging.getLogger(__name__)

EVENTS_URL = "https://gamma-api.polymarket.com/events"
TIMEOUT = 15
ARB_THRESHOLD = 1.02


def _parse_prices(market: dict) -> list[float]:
    raw = market.get("outcomePrices", '["0.5","0.5"]')
    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except Exception:
            return [0.5, 0.5]
    else:
        prices = raw if isinstance(raw, list) else [0.5, 0.5]
    try:
        return [float(p) for p in prices]
    except Exception:
        return [0.5, 0.5]


def fetch_arb_opportunities(limit: int = 100) -> list[dict]:
    """
    Fetch active Polymarket events. For each event with ≥2 markets,
    sum all YES prices. If sum > 1.02 → flag as arbitrage.
    Returns list of opportunities sorted by edge descending.
    """
    try:
        resp = requests.get(
            EVENTS_URL,
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
        events = resp.json()
    except Exception as exc:
        logger.error("[ARB] fetch failed: %s", exc)
        return []

    if not isinstance(events, list):
        return []

    opportunities = []

    for event in events:
        markets = event.get("markets", [])
        if not isinstance(markets, list) or len(markets) < 2:
            continue

        yes_prices = []
        market_names = []
        for m in markets:
            if not isinstance(m, dict):
                continue
            prices = _parse_prices(m)
            if not prices:
                continue
            yes_p = prices[0]
            if yes_p <= 0 or yes_p >= 1:
                continue
            yes_prices.append(yes_p)
            market_names.append(m.get("question", "?")[:60])

        if len(yes_prices) < 2:
            continue

        sum_yes = sum(yes_prices)
        if sum_yes <= ARB_THRESHOLD:
            continue

        edge_cents = round((sum_yes - 1.0) * 100, 2)
        slug = event.get("slug") or event.get("id", "")

        opportunities.append({
            "title": (event.get("title") or event.get("question") or "Unknown")[:80],
            "sum_yes": round(sum_yes, 4),
            "edge_cents": edge_cents,
            "yes_prices": yes_prices,
            "market_names": market_names,
            "url": f"https://polymarket.com/event/{slug}",
            "volume": float(event.get("volume", 0) or 0),
        })

    opportunities.sort(key=lambda x: x["edge_cents"], reverse=True)
    return opportunities
