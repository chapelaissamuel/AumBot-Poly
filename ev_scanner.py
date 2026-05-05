"""
ev_scanner.py — Positive EV Scanner + VIG comparator + Steam move detector.

Uses The Odds API with all bookmakers enabled.
- EV scanner: Pinnacle sharp-line method (no-vig probability)
- Vig calculator: over-round per book per match
- Line movement: SQLite odds_history, 30-min polling, steam alert
"""

import os
import math
import sqlite3
import logging
import requests
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ODDS_KEY  = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4/sports"
TIMEOUT   = 12
DB_PATH   = "odds_history.db"

# Sports to scan for EV
EV_SPORTS = [
    "soccer_epl",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_spain_la_liga",
    "basketball_nba",
    "americanfootball_nfl",
]

# Minimum edge thresholds
MIN_EV_EDGE  = 0.03   # 3% positive EV vs sharp line
MIN_ODDS     = 1.50   # don't recommend short-priced bets
STEAM_DROP   = 0.10   # 10% odds drop = steam move
STEAM_WINDOW = 3600   # within 1 hour


# ─── DB ───────────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_ev_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS odds_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id    TEXT    NOT NULL,
                match       TEXT    NOT NULL,
                sport       TEXT    NOT NULL,
                market      TEXT    NOT NULL,
                outcome     TEXT    NOT NULL,
                bookmaker   TEXT    NOT NULL,
                odds        REAL    NOT NULL,
                timestamp   TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_match_ts ON odds_history (match_id, outcome, timestamp)")
        conn.commit()
    logger.info("[EV] odds_history.db initialised")


# ─── Odds fetcher ─────────────────────────────────────────────────────────────

def _fetch_all_odds(sport: str, markets: str = "h2h") -> list[dict]:
    """Fetch odds for all bookmakers for a sport."""
    if not ODDS_KEY:
        return []
    try:
        r = requests.get(
            f"{ODDS_BASE}/{sport}/odds/",
            params={
                "apiKey": ODDS_KEY,
                "regions": "eu,uk,us",
                "markets": markets,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
            timeout=TIMEOUT,
        )
        if r.status_code == 401:
            logger.error("[EV] 401 — ODDS_API_KEY invalide")
            return []
        if r.status_code == 422:
            logger.warning("[EV] 422 — quota épuisé")
            return []
        if not r.ok:
            logger.warning("[EV] HTTP %s for %s", r.status_code, sport)
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except requests.exceptions.Timeout:
        logger.warning("[EV] timeout fetching %s", sport)
        return []
    except Exception as exc:
        logger.warning("[EV] fetch error %s: %s", sport, exc)
        return []


# ─── Sharp-line / no-vig probability ─────────────────────────────────────────

def _no_vig_probs(bookmakers: list[dict], outcomes: list[str]) -> dict[str, float]:
    """
    Pinnacle Sharp Line method:
    For each outcome, take the BEST odds available across all books.
    best_odds → implied_prob = 1 / best_odds (no vig estimate).
    Normalise so probs sum to 1.
    """
    best: dict[str, float] = {}
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            for oc in mkt.get("outcomes", []):
                name  = oc.get("name", "")
                price = float(oc.get("price", 1))
                if name in outcomes:
                    best[name] = max(best.get(name, 0), price)

    raw_probs = {name: (1 / odds) for name, odds in best.items() if odds > 1}
    total = sum(raw_probs.values())
    if total <= 0:
        return {}
    return {name: round(p / total, 4) for name, p in raw_probs.items()}


def _vig_for_bookmaker(bk: dict) -> float | None:
    """Over-round (vig) for a single bookmaker on h2h market."""
    for mkt in bk.get("markets", []):
        if mkt.get("key") != "h2h":
            continue
        outcomes = mkt.get("outcomes", [])
        if not outcomes:
            return None
        over_round = sum(1 / float(o.get("price", 1)) for o in outcomes) - 1
        return round(over_round, 4)
    return None


# ─── EV Scanner ───────────────────────────────────────────────────────────────

def scan_positive_ev(limit_per_sport: int = 5) -> list[dict]:
    """
    Scan all EV_SPORTS for positive EV opportunities.
    Returns top 5 bets sorted by EV descending.
    """
    if not ODDS_KEY:
        logger.warning("[EV] ODDS_API_KEY not set")
        return []

    all_ev: list[dict] = []

    for sport in EV_SPORTS:
        games = _fetch_all_odds(sport)
        for game in games[:limit_per_sport]:
            home    = game.get("home_team", "Home")
            away    = game.get("away_team", "Away")
            bks     = game.get("bookmakers", [])
            commence = game.get("commence_time", "")[:16].replace("T", " ")
            sport_label = _sport_label(sport)

            if len(bks) < 2:
                continue

            outcomes = [home, away, "Draw"]
            sharp_probs = _no_vig_probs(bks, outcomes)
            if not sharp_probs:
                continue

            # Best vig bookmaker
            best_vig_bk = None
            best_vig = 9999.0
            for bk in bks:
                vig = _vig_for_bookmaker(bk)
                if vig is not None and vig < best_vig:
                    best_vig = vig
                    best_vig_bk = bk.get("title", "")

            # Check each bookmaker vs sharp line
            for bk in bks:
                bk_name = bk.get("title", "Unknown")
                for mkt in bk.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    for oc in mkt.get("outcomes", []):
                        name  = oc.get("name", "")
                        price = float(oc.get("price", 1))
                        sharp_p = sharp_probs.get(name, 0)
                        if sharp_p <= 0 or price < MIN_ODDS:
                            continue
                        ev = round(sharp_p * price - 1.0, 4)
                        edge_vs_sharp = round((sharp_p - 1 / price) * 100, 2)
                        if ev >= MIN_EV_EDGE:
                            kelly = max(0.0, min(
                                (sharp_p * (price - 1) - (1 - sharp_p)) / (price - 1) * 0.25,
                                0.05,
                            ))
                            all_ev.append({
                                "sport":       sport_label,
                                "match":       f"{home} vs {away}",
                                "home":        home,
                                "away":        away,
                                "commence":    commence,
                                "outcome":     name,
                                "bookmaker":   bk_name,
                                "odds":        round(price, 2),
                                "sharp_prob":  round(sharp_p, 4),
                                "ev":          ev,
                                "edge":        edge_vs_sharp,
                                "kelly":       round(kelly, 4),
                                "best_vig_bk": best_vig_bk or "",
                                "best_vig":    round(best_vig * 100, 2),
                            })

    all_ev.sort(key=lambda x: x["ev"], reverse=True)
    logger.info("[EV] found %d positive EV opportunities", len(all_ev))
    return all_ev[:10]


def _sport_label(sport: str) -> str:
    labels = {
        "soccer_epl":                    "EPL ⚽",
        "soccer_france_ligue_one":        "Ligue 1 ⚽",
        "soccer_uefa_champs_league":      "UCL ⚽",
        "soccer_germany_bundesliga":      "Bundesliga ⚽",
        "soccer_italy_serie_a":           "Serie A ⚽",
        "soccer_spain_la_liga":           "La Liga ⚽",
        "basketball_nba":                 "NBA 🏀",
        "americanfootball_nfl":           "NFL 🏈",
    }
    return labels.get(sport, sport)


# ─── Fuzzy team name matching ─────────────────────────────────────────────────

# Alias table: canonical search term → list of substrings to match
_TEAM_ALIASES: dict[str, list[str]] = {
    "psg":              ["paris", "psg", "saint-germain"],
    "paris":            ["paris", "psg", "saint-germain"],
    "saint-germain":    ["paris", "psg", "saint-germain"],
    "man city":         ["manchester city", "man city"],
    "man utd":          ["manchester united", "man utd", "man united"],
    "man united":       ["manchester united", "man utd", "man united"],
    "spurs":            ["tottenham", "spurs"],
    "inter":            ["inter milan", "internazionale", "inter"],
    "atletico":         ["atletico madrid", "atletico"],
    "bayer":            ["bayer leverkusen", "bayer"],
    "rb leipzig":       ["rb leipzig", "rasenballsport"],
    "newcastle":        ["newcastle", "newcastle united"],
    "wolves":           ["wolverhampton", "wolves"],
    "brighton":         ["brighton", "hove albion"],
    "forest":           ["nottingham forest", "nottm forest", "forest"],
}


def _expand_search_terms(search: str) -> list[str]:
    """
    Given a user search string, return a list of substrings to check
    against team names. Handles known aliases (PSG → Paris / Saint-Germain).
    """
    key = search.strip().lower()
    if key in _TEAM_ALIASES:
        return _TEAM_ALIASES[key]
    # Also check if key is a substring of any alias group
    for canon, aliases in _TEAM_ALIASES.items():
        if key in aliases:
            return aliases
    # No alias — use the search term itself plus individual words
    terms = [key]
    words = [w for w in key.split() if len(w) >= 3]
    terms.extend(words)
    return terms


def _team_matches(search_terms: list[str], team_name: str) -> bool:
    """Return True if any search term is a substring of the team name."""
    name_lower = team_name.lower()
    return any(term in name_lower for term in search_terms)


# ─── VIG Comparator ───────────────────────────────────────────────────────────

def compare_vig_for_match(home: str, away: str, sport: str = "soccer_epl") -> list[dict]:
    """
    Return a per-bookmaker vig comparison for a specific match.
    Uses fuzzy substring matching so 'PSG' matches 'Paris Saint-Germain'.
    """
    home_terms = _expand_search_terms(home)
    away_terms = _expand_search_terms(away)
    games = _fetch_all_odds(sport)
    for game in games:
        gh = game.get("home_team", "")
        ga = game.get("away_team", "")
        if _team_matches(home_terms, gh) or _team_matches(away_terms, ga) or \
           _team_matches(home_terms, ga) or _team_matches(away_terms, gh):
            bks = game.get("bookmakers", [])
            results = []
            for bk in bks:
                vig = _vig_for_bookmaker(bk)
                if vig is not None:
                    results.append({"bookmaker": bk["title"], "vig": vig})
            results.sort(key=lambda x: x["vig"])
            return results
    return []


def best_odds_for_match(search: str) -> dict | None:
    """
    Find a match by keyword and return best odds per outcome across all books.
    Uses fuzzy substring matching with alias expansion:
    'PSG' matches 'Paris Saint-Germain', 'Paris', etc.
    """
    search_terms = _expand_search_terms(search)
    for sport in EV_SPORTS:
        games = _fetch_all_odds(sport)
        for game in games:
            h = game.get("home_team", "")
            a = game.get("away_team", "")
            if _team_matches(search_terms, h) or _team_matches(search_terms, a):
                bks = game.get("bookmakers", [])
                outcomes: dict[str, dict] = {}
                for bk in bks:
                    for mkt in bk.get("markets", []):
                        if mkt.get("key") != "h2h":
                            continue
                        for oc in mkt.get("outcomes", []):
                            name  = oc["name"]
                            price = float(oc.get("price", 1))
                            if name not in outcomes or price > outcomes[name]["odds"]:
                                outcomes[name] = {"odds": price, "bookmaker": bk["title"]}

                vig_results = []
                for bk in bks:
                    vig = _vig_for_bookmaker(bk)
                    if vig is not None:
                        vig_results.append(vig)
                avg_vig = sum(vig_results) / len(vig_results) if vig_results else 0

                return {
                    "home":     h,
                    "away":     a,
                    "sport":    sport,
                    "outcomes": outcomes,
                    "avg_vig":  round(avg_vig * 100, 2),
                    "n_books":  len(bks),
                }
    return None


# ─── Odds history + steam detector ───────────────────────────────────────────

def snapshot_odds():
    """
    Poll all EV_SPORTS, save current h2h odds to DB.
    Called every 30 minutes by background thread.
    """
    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for sport in EV_SPORTS[:4]:  # limit to 4 sports per poll to save API calls
        games = _fetch_all_odds(sport)
        for game in games[:5]:
            match_id = game.get("id", "")
            match    = f"{game.get('home_team','')} vs {game.get('away_team','')}"
            for bk in game.get("bookmakers", []):
                bk_name = bk.get("title", "")
                for mkt in bk.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    for oc in mkt.get("outcomes", []):
                        rows.append((
                            match_id, match, sport, "h2h",
                            oc.get("name", ""), bk_name,
                            float(oc.get("price", 0)), ts,
                        ))
    if rows:
        with _conn() as conn:
            conn.executemany(
                "INSERT INTO odds_history (match_id,match,sport,market,outcome,bookmaker,odds,timestamp) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
        logger.info("[EV] snapshot saved — %d rows", len(rows))


def detect_steam_moves(window_seconds: int = STEAM_WINDOW,
                       drop_threshold: float = STEAM_DROP) -> list[dict]:
    """
    Detect steam moves: odds dropped > threshold within window_seconds.
    Returns list of steam alerts sorted by drop descending.
    """
    since_ts = datetime.fromtimestamp(
        time.time() - window_seconds, tz=timezone.utc
    ).isoformat()

    with _conn() as conn:
        # Get earliest odds in window per (match_id, outcome)
        opens = conn.execute("""
            SELECT match_id, match, outcome, bookmaker,
                   MIN(odds) as min_odds, MAX(odds) as max_odds,
                   MIN(timestamp) as first_ts, MAX(timestamp) as last_ts
            FROM odds_history
            WHERE timestamp >= ?
            GROUP BY match_id, outcome, bookmaker
            HAVING COUNT(*) >= 2
        """, (since_ts,)).fetchall()

    moves = []
    for row in opens:
        max_odds = row["max_odds"]
        min_odds = row["min_odds"]
        if max_odds <= 0:
            continue
        drop = (max_odds - min_odds) / max_odds
        if drop >= drop_threshold:
            moves.append({
                "match":      row["match"],
                "outcome":    row["outcome"],
                "bookmaker":  row["bookmaker"],
                "open_odds":  round(max_odds, 2),
                "current_odds": round(min_odds, 2),
                "drop_pct":   round(drop * 100, 1),
                "first_ts":   row["first_ts"][:16],
                "last_ts":    row["last_ts"][:16],
            })

    moves.sort(key=lambda x: x["drop_pct"], reverse=True)
    return moves


def format_steam_alert(move: dict) -> str:
    return (
        f"🔥 *STEAM MOVE DÉTECTÉ*\n\n"
        f"Match: _{move['match']}_\n"
        f"Marché: *{move['outcome']}*\n"
        f"Cote: {move['open_odds']} → *{move['current_odds']}* "
        f"en {move['last_ts'][11:]} UTC\n"
        f"Chute: *{move['drop_pct']:.1f}%* _(sharp money)_\n"
        f"Source: {move['bookmaker']}"
    )


# ─── Background polling thread ────────────────────────────────────────────────

_steam_alert_callback = None
_poll_thread_started  = False


def set_steam_alert_callback(fn):
    global _steam_alert_callback
    _steam_alert_callback = fn


def start_odds_polling():
    """Start 30-min odds snapshot + steam detection in daemon thread."""
    global _poll_thread_started
    if _poll_thread_started:
        return
    _poll_thread_started = True

    def _loop():
        import asyncio
        while True:
            try:
                snapshot_odds()
                moves = detect_steam_moves()
                if moves and _steam_alert_callback:
                    for move in moves[:3]:
                        msg = format_steam_alert(move)
                        try:
                            loop = asyncio.new_event_loop()
                            loop.run_until_complete(_steam_alert_callback(msg))
                            loop.close()
                        except Exception as exc:
                            logger.warning("[EV] steam alert callback: %s", exc)
            except Exception as exc:
                logger.error("[EV] polling loop error: %s", exc, exc_info=True)
            time.sleep(1800)  # 30 minutes

    t = threading.Thread(target=_loop, daemon=True, name="OddsPoller")
    t.start()
    logger.info("[EV] Odds polling thread started (every 30min)")
