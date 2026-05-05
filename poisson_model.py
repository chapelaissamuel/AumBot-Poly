"""
poisson_model.py — Dixon-Coles Poisson model for football predictions.
The same mathematics used by professional bookmakers.

Feeds on real team stats from Football-Data.org + BallDontLie.
Outputs 1X2 + Over/Under + BTTS probabilities.
"""

import math
import logging
from scipy.stats import poisson  # type: ignore

logger = logging.getLogger(__name__)

# European football league averages (home/away goals per game)
AVG_HOME_GOALS = 1.36
AVG_AWAY_GOALS = 1.06


# ─── Core Poisson model ───────────────────────────────────────────────────────

def poisson_predict(
    home_attack: float,
    home_defense: float,
    away_attack: float,
    away_defense: float,
    avg_home_goals: float = AVG_HOME_GOALS,
    avg_away_goals: float = AVG_AWAY_GOALS,
) -> dict:
    """
    Dixon-Coles Poisson model.
    attack  > 1.0 = above-average attacker
    defense < 1.0 = above-average defender (concedes less)

    lambda_home = home_attack * away_defense * avg_home_goals
    lambda_away = away_attack * home_defense * avg_away_goals
    """
    lambda_home = max(0.1, home_attack * away_defense * avg_home_goals)
    lambda_away = max(0.1, away_attack * home_defense * avg_away_goals)

    home_win = draw = away_win = 0.0
    over25 = btts = over15 = 0.0

    for i in range(9):       # 0..8 home goals
        ph = poisson.pmf(i, lambda_home)
        for j in range(9):   # 0..8 away goals
            pa = poisson.pmf(j, lambda_away)
            p = ph * pa
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p
            if i + j > 2.5:
                over25 += p
            if i + j > 1.5:
                over15 += p
            if i > 0 and j > 0:
                btts += p

    return {
        "home_win":    round(home_win, 4),
        "draw":        round(draw, 4),
        "away_win":    round(away_win, 4),
        "over25":      round(over25, 4),
        "under25":     round(1 - over25, 4),
        "over15":      round(over15, 4),
        "btts":        round(btts, 4),
        "no_btts":     round(1 - btts, 4),
        "lambda_home": round(lambda_home, 3),
        "lambda_away": round(lambda_away, 3),
    }


# ─── Team rating builder ──────────────────────────────────────────────────────

def _safe_ratio(numerator: float, denominator: float, default: float = 1.0) -> float:
    return round(numerator / denominator, 4) if denominator > 0 else default


def build_team_ratings(home_form: dict, away_form: dict) -> dict:
    """
    Convert form data (goals for/against over last N matches) into
    attack/defense ratings relative to league average.

    form dict keys: gf (goals for), ga (goals against), n (games played)
    """
    # League average rates used as baseline
    lg_home = AVG_HOME_GOALS
    lg_away = AVG_AWAY_GOALS

    h_gf = home_form.get("gf", lg_home * home_form.get("n", 1))
    h_ga = home_form.get("ga", lg_away * home_form.get("n", 1))
    h_n  = max(1, home_form.get("n", 1))

    a_gf = away_form.get("gf", lg_away * away_form.get("n", 1))
    a_ga = away_form.get("ga", lg_home * away_form.get("n", 1))
    a_n  = max(1, away_form.get("n", 1))

    # Average goals per game
    h_attack  = (h_gf / h_n) / lg_home      # > 1 = above-average scorer
    h_defense = (h_ga / h_n) / lg_away      # < 1 = above-average defender
    a_attack  = (a_gf / a_n) / lg_away
    a_defense = (a_ga / a_n) / lg_home

    # Clamp to reasonable range to prevent extreme predictions
    clamp = lambda v: max(0.3, min(3.0, v))
    return {
        "home_attack":  clamp(h_attack),
        "home_defense": clamp(h_defense),
        "away_attack":  clamp(a_attack),
        "away_defense": clamp(a_defense),
    }


def ratings_from_fd_form(home_form_str: str, away_form_str: str) -> dict:
    """
    Parse Football-Data.org form string like:
    "[Premier League] Arsenal: W6D2L2/10 GF14GA8 #3 68pts GD+6 | Chelsea: W4D3L3/10 GF12GA11"
    Extract gf/ga/n for each team.
    """
    import re

    def _extract(text: str) -> dict:
        gf = ga = n = 0
        m = re.search(r"GF(\d+)GA(\d+)", text)
        if m:
            gf, ga = int(m.group(1)), int(m.group(2))
        m2 = re.search(r"W(\d+)D(\d+)L(\d+)/(\d+)", text)
        if m2:
            n = int(m2.group(4))
        return {"gf": gf, "ga": ga, "n": max(n, 1)}

    home_data = _extract(home_form_str)
    away_data = _extract(away_form_str)
    return build_team_ratings(home_data, away_data)


def ratings_default() -> dict:
    """Neutral ratings when form data is unavailable."""
    return {
        "home_attack":  1.0,
        "home_defense": 1.0,
        "away_attack":  1.0,
        "away_defense": 1.0,
    }


# ─── Edge calculation ─────────────────────────────────────────────────────────

def compute_poisson_edges(
    poisson_result: dict,
    bookmaker_odds: dict,
) -> list[dict]:
    """
    Compare Poisson probabilities vs bookmaker implied probabilities.
    bookmaker_odds: {
      "home_win": decimal_odds,
      "draw": decimal_odds,
      "away_win": decimal_odds,
      "over25": decimal_odds,   (optional)
      "btts": decimal_odds,     (optional)
    }
    Returns list of value bets with edge > 5% AND odds > 1.5.
    """
    markets = {
        "home_win":  "Victoire domicile",
        "draw":      "Match nul",
        "away_win":  "Victoire extérieur",
        "over25":    "Over 2.5 buts",
        "btts":      "Les deux marquent",
    }
    edges = []
    for key, label in markets.items():
        true_p = poisson_result.get(key, 0)
        odds   = bookmaker_odds.get(key, 0)
        if odds < 1.5 or true_p <= 0:
            continue
        imp_p = 1.0 / odds
        edge  = round((true_p - imp_p) * 100, 2)
        ev    = round(true_p * odds - 1.0, 4)
        if edge > 5.0 and ev > 0:
            k = max(0.0, min((true_p * (odds - 1) - (1 - true_p)) / (odds - 1) * 0.25, 0.05))
            edges.append({
                "market":  key,
                "label":   label,
                "true_p":  round(true_p, 4),
                "imp_p":   round(imp_p, 4),
                "odds":    odds,
                "edge":    edge,
                "ev":      round(ev, 4),
                "kelly":   round(k, 4),
            })
    edges.sort(key=lambda x: x["edge"], reverse=True)
    return edges


def format_poisson_block(
    home: str,
    away: str,
    result: dict,
    edges: list[dict],
    best_bk: str = "",
) -> str:
    """Format Poisson prediction as Telegram message block."""
    lines = [
        f"⚽ *{home} vs {away}*",
        f"🧮 Poisson: {home} *{result['home_win']:.0%}* | "
        f"Nul *{result['draw']:.0%}* | "
        f"{away} *{result['away_win']:.0%}*",
        f"📊 Over 2.5: *{result['over25']:.0%}* | "
        f"BTTS: *{result['btts']:.0%}*",
        f"λ domicile: {result['lambda_home']} | λ extérieur: {result['lambda_away']}",
    ]
    if edges:
        best = edges[0]
        lines.append(
            f"💡 *VALUE: {best['label']}* — Edge *+{best['edge']:.1f}pts*\n"
            f"   Cote: {best['odds']:.2f} | EV: {best['ev']:+.1%} | "
            f"Kelly: {best['kelly']*100:.1f}%"
        )
    else:
        lines.append("💡 Pas de value bet Poisson détecté (edge <5pts)")
    return "\n".join(lines)
