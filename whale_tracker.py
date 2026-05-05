"""
whale_tracker.py — Polymarket Smart Money Surveillance.

Fetches top profitable wallets via data-api.polymarket.com,
monitors their CLOB trades every 60s in a background thread,
fires Telegram alerts on large moves.
"""

import logging
import threading
import time
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TIMEOUT = 10
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Alert threshold: YES bet > $5 000 AND price < 50%
WHALE_MIN_AMOUNT = 5_000
WHALE_MAX_PRICE  = 0.50
POLL_INTERVAL    = 60  # seconds

_recent_whale_moves: list[dict] = []   # last 20 whale moves (thread-safe via GIL)
_seen_trade_ids: set[str] = set()      # dedup
_alert_callback = None                  # set by bot.py: async fn(msg: str)


# ─── Public API ───────────────────────────────────────────────────────────────

def set_alert_callback(fn):
    """Register an async coroutine function(msg: str) to receive Telegram alerts."""
    global _alert_callback
    _alert_callback = fn


def get_recent_moves(n: int = 5) -> list[dict]:
    return list(reversed(_recent_whale_moves))[:n]


def start_background_polling():
    """Start the whale watcher in a daemon thread."""
    t = threading.Thread(target=_poll_loop, daemon=True, name="WhaleTracker")
    t.start()
    logger.info("[WHALE] Background polling started (every %ds)", POLL_INTERVAL)


# ─── Data fetchers ────────────────────────────────────────────────────────────

_PROFILE_URLS = [
    # Try multiple known endpoints — Polymarket changes these periodically
    f"{DATA_API}/leaderboard",
    f"{DATA_API}/profiles",
    "https://polymarket.com/api/profile/list?limit=20&sortBy=profit&period=30d",
    "https://data-api.polymarket.com/leaderboard?limit=20",
]


def _get_top_wallets(limit: int = 20) -> list[dict]:
    """
    Fetch top wallets by 30-day profit from Polymarket leaderboard.
    Tries multiple known endpoints and gracefully falls back.
    Returns wallets with win_rate > 0.55 AND profit_30d > 500.
    """
    raw_profiles: list = []

    for url in _PROFILE_URLS:
        try:
            r = requests.get(url, timeout=TIMEOUT)
            if r.ok:
                data = r.json()
                if isinstance(data, list):
                    raw_profiles = data
                elif isinstance(data, dict):
                    for key in ("data", "results", "leaderboard", "profiles", "users"):
                        if isinstance(data.get(key), list) and data[key]:
                            raw_profiles = data[key]
                            break
                if raw_profiles:
                    logger.info("[WHALE] profiles loaded from %s (%d rows)", url, len(raw_profiles))
                    break
            else:
                logger.debug("[WHALE] %s → HTTP %s", url, r.status_code)
        except Exception as exc:
            logger.debug("[WHALE] %s → error: %s", url, exc)

    if not raw_profiles:
        logger.warning("[WHALE] no profile endpoint responded — whale tracker inactive this cycle")
        return []

    filtered = []
    for p in raw_profiles[:limit]:
        # Try every known field name variant
        wr = float(
            p.get("winRate") or p.get("win_rate") or p.get("pnlPerTrade", 0) or 0
        )
        # Normalise: if it looks like a percentage (e.g. 65.0), convert
        if wr > 1:
            wr = wr / 100.0
        profit = float(
            p.get("profit") or p.get("profit_30d") or p.get("pnl") or
            p.get("profitAndLoss") or 0
        )
        addr = (
            p.get("proxyWallet") or p.get("address") or
            p.get("wallet") or p.get("id") or ""
        )
        if addr and wr > 0.55 and profit > 500:
            filtered.append({
                "address": addr,
                "win_rate": wr,
                "profit_30d": profit,
                "name": p.get("name") or p.get("username") or addr[:8],
            })

    logger.info("[WHALE] %d qualifying wallets (wr>55%%, profit>$500)", len(filtered))
    return filtered


def _get_wallet_trades(address: str) -> list[dict]:
    """Fetch recent CLOB trades for a wallet."""
    try:
        r = requests.get(
            f"{CLOB_API}/trades",
            params={"maker_address": address, "limit": 10},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return []
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as exc:
        logger.debug("[WHALE] trades fetch error for %s: %s", address[:8], exc)
        return []


def _get_market_question(condition_id: str) -> str:
    """Resolve a condition_id to a human-readable question."""
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id, "limit": 1},
            timeout=TIMEOUT,
        )
        if r.ok:
            items = r.json() if isinstance(r.json(), list) else r.json().get("markets", [])
            if items:
                return items[0].get("question", condition_id[:40])
    except Exception:
        pass
    return condition_id[:40]


def _get_token_price(token_id: str) -> float:
    """Fetch current YES mid-price for a token."""
    try:
        r = requests.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=TIMEOUT,
        )
        if r.ok:
            return float(r.json().get("mid", 0.5))
    except Exception:
        pass
    return 0.5


# ─── Alert formatting ─────────────────────────────────────────────────────────

def _format_alert(trade: dict, wallet: dict, question: str, price: float) -> str:
    amount = float(trade.get("size", trade.get("amount", 0)) or 0)
    side = trade.get("side", "BUY").upper()
    ts = trade.get("created_at", trade.get("timestamp", ""))[:16]
    addr = wallet["address"]
    short = addr[:6] + "…" + addr[-4:]
    return (
        f"🐋 *WHALE ALERT*\n\n"
        f"Wallet: `{short}`\n"
        f"Win rate: {wallet['win_rate']:.0%} | Profit 30j: ${wallet['profit_30d']:,.0f}\n\n"
        f"Marché: _{question}_\n"
        f"Side: *{side}* | Montant: *${amount:,.0f}*\n"
        f"Prix actuel: *{price:.0%}*\n"
        f"Heure: {ts} UTC"
    )


# ─── Background loop ──────────────────────────────────────────────────────────

def _poll_loop():
    """Main polling loop — runs forever in daemon thread."""
    import asyncio

    wallets: list[dict] = []
    wallet_refresh = 0

    while True:
        try:
            now = time.time()

            # Refresh wallet list every 10 minutes
            if now - wallet_refresh > 600 or not wallets:
                wallets = _get_top_wallets()
                wallet_refresh = now

            for wallet in wallets:
                trades = _get_wallet_trades(wallet["address"])
                for trade in trades:
                    tid = trade.get("id", trade.get("transactionHash", ""))
                    if not tid or tid in _seen_trade_ids:
                        continue

                    amount = float(trade.get("size", trade.get("amount", 0)) or 0)
                    side = trade.get("side", "").upper()
                    token_id = trade.get("asset_id", trade.get("token_id", ""))
                    condition_id = trade.get("condition_id", token_id)

                    price = _get_token_price(token_id) if token_id else 0.5

                    # Store all significant trades
                    if amount >= 1000:
                        question = _get_market_question(condition_id)
                        move = {
                            "id": tid,
                            "wallet": wallet["address"][:8],
                            "win_rate": wallet["win_rate"],
                            "profit_30d": wallet["profit_30d"],
                            "question": question,
                            "side": side,
                            "amount": amount,
                            "price": price,
                            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        }
                        _recent_whale_moves.append(move)
                        if len(_recent_whale_moves) > 20:
                            _recent_whale_moves.pop(0)
                        _seen_trade_ids.add(tid)

                    # Alert only on large YES bets below 50%
                    if (
                        amount >= WHALE_MIN_AMOUNT
                        and side in ("BUY", "YES")
                        and price < WHALE_MAX_PRICE
                        and _alert_callback
                    ):
                        question = _get_market_question(condition_id)
                        msg = _format_alert(trade, wallet, question, price)
                        try:
                            loop = asyncio.new_event_loop()
                            loop.run_until_complete(_alert_callback(msg))
                            loop.close()
                        except Exception as exc:
                            logger.warning("[WHALE] alert callback error: %s", exc)

        except Exception as exc:
            logger.error("[WHALE] poll_loop error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)
