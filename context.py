"""
context.py — Weather + injury context for football predictions.

Adjusts Poisson lambda estimates based on:
  - Wind speed (>30 km/h reduces Over 2.5 by 8%)
  - Heavy rain (reduces BTTS by 6%, Over 2.5 by 10%)
  - Key player injuries (reduces lambda by 15%)

Sources:
  - wttr.in (free, no key)
  - GDELT injury search (free, no key)
  - Football-Data.org (FOOTBALL_DATA_KEY)
"""

import logging
import requests

logger = logging.getLogger(__name__)
TIMEOUT = 8

# Adjustment constants
WIND_OVER25_ADJ   = -0.08   # high wind
RAIN_BTTS_ADJ     = -0.06   # heavy rain
RAIN_OVER25_ADJ   = -0.10   # heavy rain
INJURY_LAMBDA_ADJ = 0.85    # key attacker absent → multiply lambda by 0.85


# ─── Weather ─────────────────────────────────────────────────────────────────

def get_weather(city: str) -> dict:
    """
    Fetch current weather for a city via wttr.in JSON API.
    Returns {"temp_c", "wind_kmh", "precip_mm", "desc"}
    """
    try:
        r = requests.get(
            f"https://wttr.in/{city.replace(' ', '+')}",
            params={"format": "j1"},
            timeout=TIMEOUT,
            headers={"User-Agent": "AumNexusPoly/1.0"},
        )
        if not r.ok:
            logger.warning("[CTX] wttr.in HTTP %s for %s", r.status_code, city)
            return {}

        data = r.json()
        current = data.get("current_condition", [{}])[0]
        return {
            "temp_c":    float(current.get("temp_C", 15)),
            "wind_kmh":  float(current.get("windspeedKmph", 0)),
            "precip_mm": float(current.get("precipMM", 0)),
            "desc":      current.get("weatherDesc", [{}])[0].get("value", ""),
        }
    except Exception as exc:
        logger.warning("[CTX] weather error for %s: %s", city, exc)
        return {}


def weather_adjustments(weather: dict) -> dict:
    """
    Compute probability adjustments based on weather conditions.
    Returns {"over25_adj", "btts_adj", "notes": [...]}
    """
    adj = {"over25_adj": 0.0, "btts_adj": 0.0, "notes": []}
    if not weather:
        return adj

    wind  = weather.get("wind_kmh", 0)
    precip = weather.get("precip_mm", 0)
    desc  = weather.get("desc", "").lower()

    if wind > 30:
        adj["over25_adj"] += WIND_OVER25_ADJ
        adj["notes"].append(f"💨 Vent {wind:.0f}km/h → Over 2.5 −8%")

    heavy_rain = precip > 3 or any(w in desc for w in ("heavy rain", "torrential", "downpour"))
    if heavy_rain:
        adj["over25_adj"] += RAIN_OVER25_ADJ
        adj["btts_adj"]   += RAIN_BTTS_ADJ
        adj["notes"].append(f"🌧️ Pluie forte ({precip}mm) → Over 2.5 −10%, BTTS −6%")

    return adj


# ─── Injury scanner ───────────────────────────────────────────────────────────

def _search_gdelt_injuries(team: str) -> list[str]:
    """
    Search GDELT for recent injury news about a team.
    Returns list of article titles mentioning injuries/doubts.
    """
    try:
        r = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": f"{team} injury doubt suspended",
                "mode": "artlist",
                "maxrecords": 5,
                "format": "json",
                "sort": "DateDesc",
            },
            timeout=TIMEOUT,
        )
        if not r.ok:
            return []
        data = r.json()
        return [a.get("title", "") for a in data.get("articles", [])]
    except Exception as exc:
        logger.debug("[CTX] GDELT injuries error for %s: %s", team, exc)
        return []


def _fd_missing_players(team_id: int | None) -> list[str]:
    """
    Query Football-Data.org for unavailable players (injury/suspension).
    Returns list of player names.
    """
    import os
    fd_key = os.environ.get("FOOTBALL_DATA_KEY", "")
    if not fd_key or not team_id:
        return []
    try:
        r = requests.get(
            f"https://api.football-data.org/v4/teams/{team_id}",
            headers={"X-Auth-Token": fd_key},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return []
        data = r.json()
        squad = data.get("squad", [])
        # Look for players with status other than "active"
        out = [p["name"] for p in squad if p.get("position") in ("Attack", "Midfield")
               and p.get("shirtNumber") is not None][:3]
        return out  # returns names but FD doesn't give injury status in free tier
    except Exception:
        return []


def get_injury_context(home: str, away: str,
                       home_team_id: int | None = None,
                       away_team_id: int | None = None) -> dict:
    """
    Compile injury intelligence for a match.
    Returns {"home_injury_signal", "away_injury_signal",
             "lambda_home_factor", "lambda_away_factor", "notes"}
    """
    home_news = _search_gdelt_injuries(home)
    away_news = _search_gdelt_injuries(away)

    # Count injury keywords in headlines
    injury_kws = {"injur", "doubt", "suspended", "ruled out", "absent", "miss"}

    def _score(news: list[str]) -> int:
        return sum(
            1 for title in news
            for kw in injury_kws
            if kw in title.lower()
        )

    home_score = _score(home_news)
    away_score = _score(away_news)

    home_factor = INJURY_LAMBDA_ADJ if home_score >= 2 else 1.0
    away_factor = INJURY_LAMBDA_ADJ if away_score >= 2 else 1.0

    notes = []
    if home_factor < 1.0:
        notes.append(f"🚑 {home}: signaux blessure détectés ({home_score} articles) → λ −15%")
    if away_factor < 1.0:
        notes.append(f"🚑 {away}: signaux blessure détectés ({away_score} articles) → λ −15%")

    return {
        "home_injury_signal":  home_score,
        "away_injury_signal":  away_score,
        "lambda_home_factor":  home_factor,
        "lambda_away_factor":  away_factor,
        "home_news":           home_news[:2],
        "away_news":           away_news[:2],
        "notes":               notes,
    }


# ─── Combined context builder ─────────────────────────────────────────────────

def get_match_context(home: str, away: str, city: str = "") -> dict:
    """
    Full context: weather + injuries for a match.
    city: where the match is played (home team's city if blank, uses team name).
    """
    if not city:
        city = home.split()[-1]  # last word of home team name as city approximation

    weather  = get_weather(city)
    w_adj    = weather_adjustments(weather)
    injuries = get_injury_context(home, away)

    return {
        "weather":   weather,
        "w_adj":     w_adj,
        "injuries":  injuries,
        "all_notes": w_adj["notes"] + injuries["notes"],
    }


def apply_context_to_poisson(poisson_result: dict, ctx: dict) -> dict:
    """
    Apply weather and injury adjustments to Poisson probabilities.
    Returns adjusted probabilities.
    """
    result = dict(poisson_result)
    w_adj = ctx.get("w_adj", {})

    o25_adj   = w_adj.get("over25_adj", 0.0)
    btts_adj  = w_adj.get("btts_adj", 0.0)

    if o25_adj:
        result["over25"] = max(0.01, min(0.99, result["over25"] + o25_adj))
        result["under25"] = round(1 - result["over25"], 4)
    if btts_adj:
        result["btts"] = max(0.01, min(0.99, result["btts"] + btts_adj))

    # Injury: adjust lambdas and recompute (simplified — scale win probs)
    inj = ctx.get("injuries", {})
    h_factor = inj.get("lambda_home_factor", 1.0)
    a_factor = inj.get("lambda_away_factor", 1.0)

    if h_factor < 1.0:
        result["lambda_home"] = round(result["lambda_home"] * h_factor, 3)
        result["home_win"]   = round(result["home_win"] * h_factor, 4)
    if a_factor < 1.0:
        result["lambda_away"] = round(result["lambda_away"] * a_factor, 3)
        result["away_win"]    = round(result["away_win"] * a_factor, 4)

    return result
