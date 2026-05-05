"""
parlay.py — Parlay / combiné optimizer.

Takes the top EV bets from ev_scanner, computes combined EV,
and recommends the optimal 2-3 leg parlay.
Only combines legs with POSITIVE individual EV.
"""

import logging
import itertools
from ev_scanner import scan_positive_ev

logger = logging.getLogger(__name__)

MAX_LEGS  = 3
MIN_LEGS  = 2
MIN_COMBINED_EV = 0.05   # require >5% combined EV


def compute_parlay_ev(legs: list[dict]) -> dict:
    """
    Compute combined EV for a set of independent legs.
    combined_ev = (prod of true_probs * prod of odds) - 1
    """
    combined_prob = 1.0
    combined_odds = 1.0
    for leg in legs:
        combined_prob *= leg["sharp_prob"]
        combined_odds *= leg["odds"]

    combined_ev   = round(combined_prob * combined_odds - 1.0, 4)
    kelly_combined = max(0.0, min(
        (combined_prob * (combined_odds - 1) - (1 - combined_prob)) / (combined_odds - 1) * 0.25,
        0.02,  # cap at 2% for parlays — higher variance
    ))

    return {
        "legs":           legs,
        "combined_prob":  round(combined_prob, 4),
        "combined_odds":  round(combined_odds, 2),
        "combined_ev":    combined_ev,
        "kelly":          round(kelly_combined, 4),
        "n_legs":         len(legs),
    }


def find_best_parlay(top_n_bets: int = 8) -> dict | None:
    """
    Fetch top EV bets, try all 2-leg and 3-leg combinations,
    return the parlay with the highest positive EV.
    Only combines bets on different matches (independence assumption).
    """
    bets = scan_positive_ev(limit_per_sport=3)
    if len(bets) < MIN_LEGS:
        logger.info("[PARLAY] not enough +EV bets (%d) to build parlay", len(bets))
        return None

    candidates = bets[:top_n_bets]
    best_parlay = None
    best_ev     = -999.0

    for n in range(MIN_LEGS, MAX_LEGS + 1):
        for combo in itertools.combinations(candidates, n):
            # Ensure all legs are on different matches
            matches = {leg["match"] for leg in combo}
            if len(matches) < len(combo):
                continue  # duplicate match — skip
            result = compute_parlay_ev(list(combo))
            if result["combined_ev"] > best_ev and result["combined_ev"] >= MIN_COMBINED_EV:
                best_ev     = result["combined_ev"]
                best_parlay = result

    if best_parlay:
        logger.info("[PARLAY] best parlay: %d legs, odds=%.2f, EV=+%.1f%%",
                    best_parlay["n_legs"],
                    best_parlay["combined_odds"],
                    best_parlay["combined_ev"] * 100)
    else:
        logger.info("[PARLAY] no parlay found with EV ≥ %.0f%%", MIN_COMBINED_EV * 100)

    return best_parlay


def format_parlay(parlay: dict) -> str:
    if not parlay:
        return "📭 Aucun combiné avec EV positif trouvé en ce moment."

    lines = [
        f"💰 *COMBINÉ OPTIMAL — {parlay['n_legs']} sélections*\n"
    ]
    for i, leg in enumerate(parlay["legs"], 1):
        lines.append(
            f"*Sél. {i}:* {leg['outcome']} ({leg['match'][:40]})\n"
            f"   📚 {leg['bookmaker']} @{leg['odds']:.2f} | "
            f"Sharp: {leg['sharp_prob']:.0%} | EV: {leg['ev']:+.1%}"
        )

    lines += [
        f"\n🎯 Cote combinée: *{parlay['combined_odds']:.2f}*",
        f"📈 EV combiné: *{parlay['combined_ev']:+.1%}*",
        f"💰 Mise conseillée: *{parlay['kelly']*100:.1f}% bankroll*",
        f"⚠️ _Parlays = variance élevée. PAPER TRADING uniquement._",
    ]
    return "\n".join(lines)
