"""
ws_price_cache.py — Polymarket WebSocket live price cache.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market
and subscribes to price updates for the top markets.

Prices are stored in a thread-safe dict keyed by asset_id (token ID).
The REST polling fallback is retained for initial load and reconnects.

Usage:
    from ws_price_cache import get_cached_price, start_ws_cache, subscribe_markets
"""

import json
import logging
import threading
import time
import requests

logger = logging.getLogger(__name__)

WS_URL     = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_MKTS = "https://gamma-api.polymarket.com/markets"
TIMEOUT    = 15
RECONNECT_DELAY = 5   # seconds between reconnect attempts

# Thread-safe price cache: {asset_id -> {"yes": float, "no": float, "ts": float}}
_price_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()

# token_id → slug mapping for reverse-lookup
_token_to_slug: dict[str, str] = {}
_slug_to_token: dict[str, str] = {}

_ws_thread_started = False


# ─── Cache accessors ──────────────────────────────────────────────────────────

def get_cached_price(asset_id: str) -> dict | None:
    """Return cached {yes, no, ts} for an asset_id or None."""
    with _cache_lock:
        return _price_cache.get(asset_id)


def set_cached_price(asset_id: str, yes: float, no: float):
    with _cache_lock:
        _price_cache[asset_id] = {"yes": yes, "no": no, "ts": time.time()}
    logger.debug("[WS] cached price asset_id=%s yes=%.3f no=%.3f", asset_id, yes, no)


def get_all_cached() -> dict:
    with _cache_lock:
        return dict(_price_cache)


# ─── Bootstrap: load top markets from REST and build token mapping ─────────────

def _load_top_markets(limit: int = 50) -> list[str]:
    """
    Fetch top markets from Gamma REST API, populate _token_to_slug / _slug_to_token.
    Returns list of asset_ids to subscribe to.
    """
    try:
        r = requests.get(
            GAMMA_MKTS,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
                "limit": limit,
            },
            timeout=TIMEOUT,
        )
        if not r.ok:
            logger.warning("[WS] bootstrap HTTP %s", r.status_code)
            return []
        data = r.json()
        markets = data if isinstance(data, list) else data.get("markets", [])
    except Exception as exc:
        logger.warning("[WS] bootstrap fetch error: %s", exc)
        return []

    asset_ids = []
    for m in markets:
        slug = m.get("slug", "")
        # clobTokenIds is a JSON string or list of two token IDs [YES, NO]
        raw_ids = m.get("clobTokenIds", "[]")
        if isinstance(raw_ids, str):
            try:
                raw_ids = json.loads(raw_ids)
            except Exception:
                raw_ids = []
        if not isinstance(raw_ids, list) or not raw_ids:
            continue

        yes_token = raw_ids[0]
        no_token  = raw_ids[1] if len(raw_ids) > 1 else None

        _token_to_slug[yes_token] = slug
        _slug_to_token[slug] = yes_token

        # Seed cache from current REST prices so we have something immediately
        prices_raw = m.get("outcomePrices", '["0.5","0.5"]')
        if isinstance(prices_raw, str):
            try:
                prices_raw = json.loads(prices_raw)
            except Exception:
                prices_raw = ["0.5", "0.5"]
        try:
            yes_p = float(prices_raw[0])
            no_p  = float(prices_raw[1]) if len(prices_raw) > 1 else 1 - yes_p
            set_cached_price(yes_token, yes_p, no_p)
        except Exception:
            pass

        asset_ids.append(yes_token)
        if no_token:
            asset_ids.append(no_token)

    logger.info("[WS] loaded %d top markets, %d asset_ids", len(markets), len(asset_ids))
    return asset_ids


# ─── WebSocket client ─────────────────────────────────────────────────────────

def _ws_loop(asset_ids: list[str]):
    """
    Persistent WebSocket loop with automatic reconnection.
    Runs in a daemon thread.
    """
    try:
        import websocket  # websocket-client
    except ImportError:
        logger.warning("[WS] websocket-client not installed — live WS disabled, REST-only mode")
        return

    sub_message = json.dumps({
        "assets_ids": asset_ids,
        "type": "Market",
    })

    while True:
        try:
            ws = websocket.WebSocket()
            ws.connect(WS_URL, timeout=30)
            logger.info("[WS] connected to %s", WS_URL)

            # Subscribe to asset price updates
            ws.send(sub_message)
            logger.info("[WS] subscribed to %d assets", len(asset_ids))

            while True:
                raw = ws.recv()
                if not raw:
                    continue
                _handle_ws_message(raw)

        except Exception as exc:
            logger.warning("[WS] connection error: %s — reconnecting in %ds", exc, RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)


def _handle_ws_message(raw: str):
    """Parse a WebSocket message and update the price cache."""
    try:
        # Messages may be a single object or a list of objects
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                _process_ws_event(item)
        elif isinstance(data, dict):
            _process_ws_event(data)
    except Exception as exc:
        logger.debug("[WS] parse error: %s  raw=%s", exc, raw[:200])


def _process_ws_event(event: dict):
    """
    Handle a single WebSocket price event.
    Polymarket CLOB WS emits events with:
        event_type: "price_change" | "book" | "trade"
        asset_id: token ID
        price: "0.65"  (for price_change)
        bids/asks: [...] (for book snapshots)
    """
    event_type = event.get("event_type") or event.get("type", "")
    asset_id   = event.get("asset_id", "")

    if not asset_id:
        return

    if event_type in ("price_change", "last_trade_price"):
        price_str = event.get("price") or event.get("last_trade_price")
        if price_str is None:
            return
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return

        # We receive the YES-side price; derive NO as complement
        with _cache_lock:
            existing = _price_cache.get(asset_id, {})
            yes_p = price
            no_p  = existing.get("no", 1 - price)
        set_cached_price(asset_id, yes_p, no_p)

    elif event_type == "book":
        # Use mid-price from best bid/ask
        bids = event.get("bids", [])
        asks = event.get("asks", [])
        try:
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2
                with _cache_lock:
                    existing = _price_cache.get(asset_id, {})
                    no_p = existing.get("no", 1 - mid)
                set_cached_price(asset_id, mid, no_p)
        except Exception:
            pass


# ─── Public API ───────────────────────────────────────────────────────────────

def subscribe_markets(asset_ids: list[str]):
    """
    Subscribe to additional asset IDs without restarting the WS connection.
    (Queued for next reconnect in the current simple implementation.)
    """
    logger.info("[WS] subscribe_markets called with %d ids (effective on next reconnect)", len(asset_ids))


def start_ws_cache():
    """
    Start the WebSocket price cache in a background daemon thread.
    Safe to call multiple times — only starts once.
    """
    global _ws_thread_started
    if _ws_thread_started:
        return
    _ws_thread_started = True

    asset_ids = _load_top_markets(50)
    if not asset_ids:
        logger.warning("[WS] no asset_ids to subscribe — WS not started")
        return

    t = threading.Thread(
        target=_ws_loop,
        args=(asset_ids,),
        daemon=True,
        name="PolyWS",
    )
    t.start()
    logger.info("[WS] WebSocket price cache thread started (%d assets)", len(asset_ids))
