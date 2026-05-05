"""
clob_pressure.py — Polymarket CLOB order book pressure.

Fetches the live order book for a market token and computes
bid/ask depth ratio as a directional signal.

Public endpoint, no auth required:
GET https://clob.polymarket.com/book?token_id={token_id}
"""

import logging
import requests

logger = logging.getLogger(__name__)
TIMEOUT = 8

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com/markets"


def _get_token_id(condition_id: str) -> str | None:
    """Resolve a Gamma market conditionId to a CLOB YES token_id."""
    try:
        r = requests.get(
            GAMMA_API,
            params={"conditionId": condition_id, "limit": 1},
            timeout=TIMEOUT,
        )
        if r.ok:
            items = r.json() if isinstance(r.json(), list) else r.json().get("markets", [])
            if items:
                tokens = items[0].get("clobTokenIds", "[]")
                import json
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if tokens:
                    return str(tokens[0])  # YES token is index 0
    except Exception as exc:
        logger.debug("[CLOB] get_token_id error: %s", exc)
    return None


def get_order_book_pressure(token_id: str) -> dict:
    """
    Fetch CLOB order book for YES token.
    Returns:
      bid_depth   — total USDC on buy side
      ask_depth   — total USDC on sell side
      pressure    — bid / (bid + ask), 0–1
      signal      — "BULL" | "BEAR" | "NEUTRAL"
      label       — human-readable string
    """
    try:
        r = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=TIMEOUT,
        )
        if not r.ok:
            logger.warning("[CLOB] book HTTP %s for token %s…", r.status_code, token_id[:8])
            return {}

        book = r.json()
        bids = book.get("bids", [])  # list of {price, size}
        asks = book.get("asks", [])

        bid_depth = sum(float(b.get("price", 0)) * float(b.get("size", 0)) for b in bids)
        ask_depth = sum(float(a.get("price", 0)) * float(a.get("size", 0)) for a in asks)
        total = bid_depth + ask_depth

        if total < 1:
            return {"signal": "NEUTRAL", "label": "Order book vide", "pressure": 0.5,
                    "bid_depth": 0, "ask_depth": 0}

        pressure = bid_depth / total

        if pressure > 0.70:
            signal = "BULL"
            label = f"📗 ORDER BOOK: {pressure:.0%} buy pressure — signal BULL fort"
        elif pressure < 0.30:
            signal = "BEAR"
            label = f"📕 ORDER BOOK: {pressure:.0%} buy pressure — signal BEAR fort"
        else:
            signal = "NEUTRAL"
            label = f"📊 ORDER BOOK: {pressure:.0%} buy pressure — équilibré"

        return {
            "signal": signal,
            "label": label,
            "pressure": round(pressure, 3),
            "bid_depth": round(bid_depth, 2),
            "ask_depth": round(ask_depth, 2),
        }

    except Exception as exc:
        logger.warning("[CLOB] get_order_book_pressure error: %s", exc)
        return {}


def get_pressure_for_market(market: dict) -> dict:
    """
    Convenience wrapper: given a scored market dict (with conditionId or clobTokenIds),
    resolve the token_id and return pressure data.
    """
    import json

    # Try to get token_id directly
    tokens_raw = market.get("clobTokenIds", "")
    if isinstance(tokens_raw, str):
        try:
            tokens = json.loads(tokens_raw)
        except Exception:
            tokens = []
    else:
        tokens = tokens_raw if isinstance(tokens_raw, list) else []

    token_id = str(tokens[0]) if tokens else None

    # Fallback: resolve from conditionId
    if not token_id:
        cid = market.get("conditionId", "")
        if cid:
            token_id = _get_token_id(cid)

    if not token_id:
        return {"signal": "NEUTRAL", "label": "Token ID introuvable", "pressure": 0.5,
                "bid_depth": 0, "ask_depth": 0}

    return get_order_book_pressure(token_id)
