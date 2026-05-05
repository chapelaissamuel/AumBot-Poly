"""
Sports value bets — EPL + NBA via The Odds API.
Fetches live h2h odds, converts to implied prob, calls LLM for true prob,
returns top 3 by edge.
"""
import os
import json
import logging
import requests
from requests.exceptions import Timeout as RequestsTimeout
from llm import llm_call

logger = logging.getLogger(__name__)

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4/sports"
SPORTS = ["soccer_epl", "basketball_nba"]
TIMEOUT = 10  # seconds — hard limit on every request


class OddsApiError(Exception):
    """Raised for known Odds API failure modes."""
    def __init__(self, kind: str, message: str):
        self.kind = kind  # "auth" | "quota" | "timeout" | "unknown"
        super().__init__(message)


def _log_key_hint():
    """Log first 4 chars of ODDS_API_KEY for verification (never full key)."""
    key = ODDS_API_KEY
    if key:
        logger.info("[SPORTS] ODDS_API_KEY prefix: %s…", key[:4])
    else:
        logger.warning("[SPORTS] ODDS_API_KEY is empty or not set")


def _fetch_odds(sport: str) -> list:
    """
    Fetch h2h odds for a sport.
    Raises OddsApiError on 401 (invalid key), 422 (quota), or timeout.
    Returns list of games on success.
    """
    if not ODDS_API_KEY:
        raise OddsApiError("auth", "ODDS_API_KEY not configured")

    try:
        resp = requests.get(
            f"{ODDS_BASE}/{sport}/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
            timeout=TIMEOUT,
        )
    except RequestsTimeout:
        raise OddsApiError("timeout", f"The Odds API timeout after {TIMEOUT}s ({sport})")
    except requests.RequestException as exc:
        raise OddsApiError("unknown", f"Request failed: {exc}")

    if resp.status_code == 401:
        raise OddsApiError("auth", f"401 — ODDS_API_KEY invalide ({sport})")
    if resp.status_code == 422:
        raise OddsApiError("quota", f"422 — quota épuisé ({sport})")
    if not resp.ok:
        raise OddsApiError("unknown", f"HTTP {resp.status_code} from Odds API ({sport})")

    data = resp.json()
    return data if isinstance(data, list) else []


def _best_odds(bookmakers: list, team: str) -> float:
    """Return best decimal odds for a team across bookmakers."""
    best = 0.0
    for bk in bookmakers:
        for market in bk.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                if outcome.get("name") == team:
                    odds = float(outcome.get("price", 0))
                    if odds > best:
                        best = odds
    return best


def _llm_true_prob(home: str, away: str, sport: str,
                   imp_home: float, imp_away: float) -> tuple[float, str]:
    """Ask LLM for true probability of home win. Returns (prob, reasoning)."""
    prompt = (
        f"You are a calibrated sports analyst. Sport: {sport}.\n"
        f"Match: {away} @ {home}\n"
        f"Market implied probabilities — Home: {imp_home:.1%}, Away: {imp_away:.1%}\n\n"
        f"Estimate the TRUE probability that the home team ({home}) wins.\n"
        f"Account for home advantage, current form, and market biases.\n\n"
        f'Return ONLY valid JSON: {{"true_probability": <float 0-1>, "reasoning": "<2 sentences max>"}}'
    )
    raw = llm_call(prompt, max_tokens=200)
    try:
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end <= 0:
            return imp_home, "LLM parse error."
        data = json.loads(raw[start:end])
        prob = max(0.01, min(0.99, float(data.get("true_probability", imp_home))))
        return prob, data.get("reasoning", "")
    except Exception:
        return imp_home, "LLM parse error — using market price."


def kelly(true_p: float, implied_p: float) -> float:
    """Quarter-Kelly capped at 5%."""
    if implied_p <= 0 or implied_p >= 1:
        return 0.0
    odds = (1.0 - implied_p) / implied_p
    fk = (true_p * odds - (1.0 - true_p)) / odds
    return round(max(0.0, min(fk * 0.25, 0.05)), 4)


def get_value_bets(limit_per_sport: int = 5) -> list[dict]:
    """
    Fetch EPL + NBA odds, compare market vs LLM probability.
    Raises OddsApiError on API failures (caller handles messaging).
    Returns top 3 value bets sorted by absolute edge.
    """
    _log_key_hint()
    all_bets = []

    for sport in SPORTS:
        # Let OddsApiError propagate to the caller
        games = _fetch_odds(sport)
        sport_label = "NBA 🏀" if "basketball" in sport else "EPL ⚽"

        for game in games[:limit_per_sport]:
            home = game.get("home_team", "Home")
            away = game.get("away_team", "Away")
            bookmakers = game.get("bookmakers", [])

            if not bookmakers:
                continue

            odds_home = _best_odds(bookmakers, home)
            odds_away = _best_odds(bookmakers, away)

            if odds_home < 1.01 or odds_away < 1.01:
                continue

            imp_home = 1.0 / odds_home
            imp_away = 1.0 / odds_away

            true_home, reasoning = _llm_true_prob(home, away, sport_label, imp_home, imp_away)
            edge = round((true_home - imp_home) * 100, 1)
            k = kelly(true_home, imp_home)

            all_bets.append({
                "sport": sport_label,
                "home": home,
                "away": away,
                "imp_home": imp_home,
                "true_home": true_home,
                "odds_home": odds_home,
                "edge": edge,
                "kelly": k,
                "reasoning": reasoning,
                "commence": game.get("commence_time", "")[:16].replace("T", " "),
            })

    all_bets.sort(key=lambda x: abs(x["edge"]), reverse=True)
    return all_bets[:3]
