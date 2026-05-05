"""
data_sources.py — Free data enrichment for AUM NEXUS POLY.

SOURCE 1: BallDontLie  — NBA form (no key required)
SOURCE 2: Football-Data.org — EPL/UCL/L1/BL/SA standings + form (FOOTBALL_DATA_KEY)
SOURCE 3: Polymarket Historical — base rate from resolved markets (no key)
SOURCE 4: Metaculus — geopolitical base rate from resolved questions (no key)
SOURCE 5: GDELT — real-time OSINT news context (no key)
"""

import os
import logging
import requests
from datetime import date, timedelta

logger = logging.getLogger(__name__)

TIMEOUT = 10  # seconds, all external calls

# ─── Football-Data.org competition IDs ───────────────────────────────────────
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")
FD_BASE = "https://api.football-data.org/v4"
FD_COMPETITIONS = {
    "PL":  "Premier League",
    "CL":  "Champions League",
    "FL1": "Ligue 1",
    "BL1": "Bundesliga",
    "SA":  "Serie A",
    "PD":  "La Liga",
}

# ─── BallDontLie ─────────────────────────────────────────────────────────────
BDL_BASE = "https://api.balldontlie.io/v1"


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1 — BallDontLie: NBA team form (last 10 games)
# ═══════════════════════════════════════════════════════════════════════════════

def _bdl_get(path: str, params: dict) -> list:
    try:
        r = requests.get(f"{BDL_BASE}{path}", params=params, timeout=TIMEOUT)
        if r.ok:
            return r.json().get("data", [])
        logger.warning("[BDL] HTTP %s for %s", r.status_code, path)
    except Exception as exc:
        logger.warning("[BDL] request error: %s", exc)
    return []


def _bdl_team_id(team_name: str) -> int | None:
    """Resolve an NBA team name to BallDontLie team_id."""
    teams = _bdl_get("/teams", {"search": team_name.split()[-1], "per_page": 5})
    for t in teams:
        if team_name.lower() in t.get("full_name", "").lower():
            return t["id"]
    return teams[0]["id"] if teams else None


def get_nba_form(home: str, away: str) -> str:
    """
    Return a short form string for both NBA teams over the last 10 games.
    Example: "LA Lakers: W7/10, avg pts 118.2 | Boston Celtics: W8/10, avg pts 112.4"
    """
    today = date.today().isoformat()
    start = (date.today() - timedelta(days=60)).isoformat()
    results = []

    for team_name in (home, away):
        tid = _bdl_team_id(team_name)
        if not tid:
            results.append(f"{team_name}: form N/A")
            continue

        games = _bdl_get("/games", {
            "team_ids[]": tid,
            "start_date": start,
            "end_date": today,
            "per_page": 10,
        })

        wins = 0
        pts_list = []
        for g in games:
            home_id = g.get("home_team", {}).get("id")
            home_score = g.get("home_team_score", 0)
            away_score = g.get("visitor_team_score", 0)
            is_home = home_id == tid
            my_score = home_score if is_home else away_score
            opp_score = away_score if is_home else home_score
            if my_score and opp_score:
                pts_list.append(my_score)
                if my_score > opp_score:
                    wins += 1

        n = len(games)
        if n == 0:
            results.append(f"{team_name}: no recent games")
            continue
        avg_pts = sum(pts_list) / len(pts_list) if pts_list else 0
        results.append(f"{team_name}: W{wins}/{n}, avg {avg_pts:.0f}pts")

    return " | ".join(results)


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 2 — Football-Data.org: EPL/UCL form + standings
# ═══════════════════════════════════════════════════════════════════════════════

def _fd_get(path: str, params: dict | None = None) -> dict:
    if not FOOTBALL_DATA_KEY:
        return {}
    try:
        r = requests.get(
            f"{FD_BASE}{path}",
            params=params or {},
            headers={"X-Auth-Token": FOOTBALL_DATA_KEY},
            timeout=TIMEOUT,
        )
        if r.ok:
            return r.json()
        logger.warning("[FD] HTTP %s for %s — %s", r.status_code, path, r.text[:100])
    except Exception as exc:
        logger.warning("[FD] request error: %s", exc)
    return {}


def _fd_team_id(team_name: str, competition: str = "PL") -> int | None:
    """Find a team ID within a competition's current standings."""
    data = _fd_get(f"/competitions/{competition}/standings")
    for group in data.get("standings", []):
        for row in group.get("table", []):
            name = row.get("team", {}).get("name", "")
            if team_name.lower() in name.lower():
                return row["team"]["id"]
    return None


def get_football_form(home: str, away: str, competition: str = "PL") -> str:
    """
    Return form string for both teams (last 10 finished matches).
    Also returns current standing position if available.
    """
    if not FOOTBALL_DATA_KEY:
        return "Football-Data: clé non configurée (FOOTBALL_DATA_KEY)"

    results = []
    # Standings for position context
    standings_data = _fd_get(f"/competitions/{competition}/standings")
    position_map = {}
    for group in standings_data.get("standings", []):
        for row in group.get("table", []):
            tid = row.get("team", {}).get("id")
            name = row.get("team", {}).get("name", "")
            pos = row.get("position", "?")
            pts = row.get("points", "?")
            gd = row.get("goalDifference", "?")
            position_map[tid] = {"name": name, "pos": pos, "pts": pts, "gd": gd}

    for team_name in (home, away):
        tid = _fd_team_id(team_name, competition)
        if not tid:
            results.append(f"{team_name}: équipe non trouvée ({competition})")
            continue

        matches = _fd_get(f"/teams/{tid}/matches", {"status": "FINISHED", "limit": 10})
        match_list = matches.get("matches", [])

        wins = draws = losses = gf = ga = 0
        for m in match_list:
            score = m.get("score", {}).get("fullTime", {})
            h_goals = score.get("home", 0) or 0
            a_goals = score.get("away", 0) or 0
            home_team_id = m.get("homeTeam", {}).get("id")
            is_home = home_team_id == tid
            my_g = h_goals if is_home else a_goals
            op_g = a_goals if is_home else h_goals
            gf += my_g
            ga += op_g
            if my_g > op_g:
                wins += 1
            elif my_g == op_g:
                draws += 1
            else:
                losses += 1

        n = len(match_list)
        standing = position_map.get(tid, {})
        pos_str = f"#{standing.get('pos','?')} {standing.get('pts','?')}pts GD{standing.get('gd',0):+}" if standing else ""
        form_str = f"W{wins}D{draws}L{losses}/{n} GF{gf}GA{ga}"
        results.append(f"{team_name}: {form_str} {pos_str}".strip())

    comp_label = FD_COMPETITIONS.get(competition, competition)
    return f"[{comp_label}] " + " | ".join(results)


def get_ucl_upcoming() -> list[dict]:
    """Fetch upcoming UCL matches."""
    data = _fd_get("/competitions/CL/matches", {"status": "SCHEDULED"})
    matches = data.get("matches", [])[:5]
    out = []
    for m in matches:
        out.append({
            "home": m.get("homeTeam", {}).get("name", "?"),
            "away": m.get("awayTeam", {}).get("name", "?"),
            "date": m.get("utcDate", "")[:10],
            "stage": m.get("stage", ""),
        })
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 3 — Polymarket Historical: base rate from resolved markets
# ═══════════════════════════════════════════════════════════════════════════════

POLY_GAMMA = "https://gamma-api.polymarket.com/markets"


def get_polymarket_base_rate(topic_keywords: list[str], limit: int = 100) -> dict:
    """
    Fetch last `limit` resolved Polymarket markets and compute base rate:
    how many resolved YES among those matching topic_keywords.
    Returns {"total": N, "yes": Y, "base_rate": 0.XX, "sample": [...5 titles]}
    """
    try:
        r = requests.get(
            POLY_GAMMA,
            params={"closed": "true", "order": "volume", "limit": limit},
            timeout=TIMEOUT,
        )
        if not r.ok:
            logger.warning("[POLY_HIST] HTTP %s", r.status_code)
            return {}
        markets = r.json() if isinstance(r.json(), list) else r.json().get("markets", [])
    except Exception as exc:
        logger.warning("[POLY_HIST] error: %s", exc)
        return {}

    kws = [k.lower() for k in topic_keywords]
    relevant = []
    for m in markets:
        q = m.get("question", "").lower()
        if any(kw in q for kw in kws):
            relevant.append(m)

    if not relevant:
        # Fall back to global base rate across all resolved
        total = len(markets)
        yes_count = sum(
            1 for m in markets
            if str(m.get("resolution", "")).lower() in ("yes", "1", "true")
        )
        return {
            "total": total,
            "yes": yes_count,
            "base_rate": round(yes_count / total, 3) if total else 0.5,
            "sample": [],
            "note": "global base rate (no topic match)",
        }

    yes_count = sum(
        1 for m in relevant
        if str(m.get("resolution", "")).lower() in ("yes", "1", "true")
    )
    total = len(relevant)
    return {
        "total": total,
        "yes": yes_count,
        "base_rate": round(yes_count / total, 3) if total else 0.5,
        "sample": [m.get("question", "")[:80] for m in relevant[:5]],
    }


def format_poly_base_rate(br: dict) -> str:
    if not br:
        return "Polymarket base rate: N/A"
    rate_pct = br["base_rate"] * 100
    note = br.get("note", "")
    sample_str = ""
    if br.get("sample"):
        sample_str = "\n  Exemples: " + " / ".join(f'"{s}"' for s in br["sample"][:3])
    return (
        f"Polymarket historique ({br['total']} marchés résolus): "
        f"YES dans {br['yes']}/{br['total']} cas = base rate {rate_pct:.0f}%"
        f"{' (' + note + ')' if note else ''}{sample_str}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 4 — Metaculus: geopolitical base rate from resolved questions
# ═══════════════════════════════════════════════════════════════════════════════

METACULUS_BASE = "https://www.metaculus.com/api2/questions/"


def get_metaculus_base_rate(topic_keywords: list[str], limit: int = 100) -> dict:
    """
    Fetch resolved Metaculus questions, filter by topic, compute base rate.
    Returns {"total": N, "yes": Y, "base_rate": 0.XX, "sample": [...]}
    """
    try:
        r = requests.get(
            METACULUS_BASE,
            params={"status": "resolved", "order_by": "-resolve_time", "limit": limit},
            timeout=TIMEOUT,
            headers={"User-Agent": "AumNexusPoly/1.0"},
        )
        if not r.ok:
            logger.warning("[METACULUS] HTTP %s", r.status_code)
            return {}
        data = r.json()
        questions = data.get("results", [])
    except Exception as exc:
        logger.warning("[METACULUS] error: %s", exc)
        return {}

    kws = [k.lower() for k in topic_keywords]
    relevant = [
        q for q in questions
        if any(kw in q.get("title", "").lower() for kw in kws)
    ]
    pool = relevant if relevant else questions

    yes_count = 0
    for q in pool:
        res = q.get("resolution")
        # Metaculus: 1.0 = yes, 0.0 = no, -1 = ambiguous/annulled
        try:
            if float(res) >= 0.5:
                yes_count += 1
        except (TypeError, ValueError):
            pass

    total = len(pool)
    return {
        "total": total,
        "yes": yes_count,
        "base_rate": round(yes_count / total, 3) if total else 0.5,
        "sample": [q.get("title", "")[:80] for q in pool[:5]],
        "matched_topic": bool(relevant),
    }


def format_metaculus_base_rate(br: dict) -> str:
    if not br:
        return "Metaculus base rate: N/A"
    rate_pct = br["base_rate"] * 100
    label = "sur topic" if br.get("matched_topic") else "global (pas de match topic)"
    return (
        f"Metaculus ({br['total']} questions résolues, {label}): "
        f"YES {br['yes']}/{br['total']} = base rate {rate_pct:.0f}%"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 5 — GDELT: real-time OSINT news context
# ═══════════════════════════════════════════════════════════════════════════════

GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


def get_gdelt_news(topic: str, max_records: int = 8) -> list[dict]:
    """
    Fetch recent news articles on `topic` from GDELT.
    Returns list of {"title": ..., "url": ..., "date": ...}
    """
    try:
        r = requests.get(
            GDELT_BASE,
            params={
                "query": topic,
                "mode": "artlist",
                "maxrecords": max_records,
                "format": "json",
                "sort": "DateDesc",
            },
            timeout=TIMEOUT,
        )
        if not r.ok:
            logger.warning("[GDELT] HTTP %s", r.status_code)
            return []
        data = r.json()
        articles = data.get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "url": a.get("url", ""),
                "date": a.get("seendate", "")[:8],
            }
            for a in articles
        ]
    except Exception as exc:
        logger.warning("[GDELT] error: %s", exc)
        return []


def format_gdelt_context(articles: list[dict]) -> str:
    if not articles:
        return "GDELT OSINT: aucun article trouvé."
    lines = [f"GDELT news récentes ({len(articles)} articles):"]
    for a in articles:
        lines.append(f"  [{a['date']}] {a['title'][:100]}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# COMPOSITE HELPERS — called by bot.py handlers
# ═══════════════════════════════════════════════════════════════════════════════

def enrich_sports_context(home: str, away: str, sport_label: str) -> str:
    """
    Build enriched context for a sports match before LLM call.
    Combines BDL (NBA) or Football-Data (EPL/UCL) form into one string.
    """
    lines = []
    if "NBA" in sport_label or "basketball" in sport_label.lower():
        form = get_nba_form(home, away)
        lines.append(f"NBA form (last 10): {form}")
    else:
        # Try EPL first, then UCL
        epl_form = get_football_form(home, away, "PL")
        lines.append(epl_form)
        if FOOTBALL_DATA_KEY:
            ucl_form = get_football_form(home, away, "CL")
            if "non trouvée" not in ucl_form:
                lines.append(ucl_form)
    return "\n".join(lines) if lines else "Contexte form: N/A"


def enrich_future_context(topic: str) -> str:
    """
    Build enriched context for /poly future: base rates + GDELT news.
    Returns a block of text ready to inject into the LLM prompt.
    """
    keywords = [w for w in topic.lower().split() if len(w) > 3][:5]

    sections = []

    # Polymarket historical base rate
    poly_br = get_polymarket_base_rate(keywords)
    sections.append(format_poly_base_rate(poly_br))

    # Metaculus geopolitical base rate
    meta_br = get_metaculus_base_rate(keywords)
    sections.append(format_metaculus_base_rate(meta_br))

    # GDELT news
    articles = get_gdelt_news(topic)
    sections.append(format_gdelt_context(articles))

    return "\n\n".join(sections)
