"""
superforecaster.py — Superforecasting Engine for AUM NEXUS POLY.

Implements the 3-step reference method:
  1. OUTSIDE VIEW  — base rate from Metaculus community predictions
  2. INSIDE VIEW   — GDELT news velocity (acceleration signal)
  3. EXTREMISATION — weighted blend (70% outside / 30% inside, unless L3 signal)

Output injected into Crystal Ball as Agent 0 before LLM analysis.
"""

import logging
import requests
import statistics
from data_sources import get_gdelt_news

logger = logging.getLogger(__name__)
TIMEOUT = 10

METACULUS_BASE = "https://www.metaculus.com/api2/questions/"
GDELT_TIMELINE = "https://api.gdeltproject.org/api/v2/doc/doc"


# ─── STEP 1 — Outside View: Metaculus community base rate ─────────────────────

def _get_metaculus_community_predictions(keywords: list[str], limit: int = 20) -> dict:
    """
    Fetch recently-resolved Metaculus questions similar to topic.
    Use community_prediction at time of resolution as the base rate signal.
    Returns {"base_rate": float, "n": int, "confidence": str}
    """
    try:
        r = requests.get(
            METACULUS_BASE,
            params={
                "status": "resolved",
                "order_by": "-resolve_time",
                "limit": limit,
                "search": " ".join(keywords[:3]),
            },
            timeout=TIMEOUT,
            headers={"User-Agent": "AumNexusPoly/1.0"},
        )
        if not r.ok:
            logger.warning("[SF] Metaculus HTTP %s", r.status_code)
            return {}

        questions = r.json().get("results", [])
        community_probs = []
        for q in questions:
            cp = q.get("community_prediction", {})
            pred = None
            if isinstance(cp, dict):
                pred = cp.get("full", {}).get("q2") or cp.get("q2")
            elif isinstance(cp, (float, int)):
                pred = float(cp)
            if pred is not None:
                try:
                    community_probs.append(float(pred))
                except (TypeError, ValueError):
                    pass

        if not community_probs:
            return {"base_rate": 0.5, "n": 0, "confidence": "low"}

        base_rate = statistics.mean(community_probs)
        n = len(community_probs)
        confidence = "high" if n >= 10 else "medium" if n >= 5 else "low"
        return {"base_rate": round(base_rate, 3), "n": n, "confidence": confidence}

    except Exception as exc:
        logger.warning("[SF] outside_view error: %s", exc)
        return {"base_rate": 0.5, "n": 0, "confidence": "low"}


# ─── STEP 2 — Inside View: GDELT news velocity signal ─────────────────────────

def _get_gdelt_timeline_volume(topic: str) -> dict:
    """
    Fetch GDELT timeline volume for topic.
    Detect if recent volume is > 2 std dev above rolling mean.
    Returns {"velocity": float, "acceleration": bool, "signal_strength": str}
    """
    try:
        r = requests.get(
            GDELT_TIMELINE,
            params={
                "query": topic,
                "mode": "timelinevolraw",
                "format": "json",
                "smoothing": 3,
            },
            timeout=TIMEOUT,
        )
        if not r.ok:
            logger.warning("[SF] GDELT timeline HTTP %s", r.status_code)
            return {"velocity": 1.0, "acceleration": False, "signal_strength": "none"}

        data = r.json()
        # GDELT returns {"timeline": [{"series": [{"value": N, "date": "..."}]}]}
        timeline = data.get("timeline", [])
        if not timeline:
            return {"velocity": 1.0, "acceleration": False, "signal_strength": "none"}

        series = timeline[0].get("data", [])
        values = [float(p.get("value", 0)) for p in series if p.get("value") is not None]

        if len(values) < 5:
            return {"velocity": 1.0, "acceleration": False, "signal_strength": "none"}

        recent = values[-3:]    # last 3 data points
        historical = values[:-3]

        recent_mean = statistics.mean(recent)
        hist_mean = statistics.mean(historical) if historical else 1
        hist_std = statistics.stdev(historical) if len(historical) > 1 else 1

        velocity = recent_mean / max(hist_mean, 1)
        z_score = (recent_mean - hist_mean) / max(hist_std, 0.1)
        acceleration = z_score > 2.0

        if z_score > 3:
            strength = "strong"
        elif z_score > 2:
            strength = "moderate"
        else:
            strength = "none"

        return {
            "velocity": round(velocity, 2),
            "acceleration": acceleration,
            "signal_strength": strength,
            "z_score": round(z_score, 2),
        }

    except Exception as exc:
        logger.warning("[SF] gdelt_timeline error: %s", exc)
        return {"velocity": 1.0, "acceleration": False, "signal_strength": "none"}


def _news_inside_view(topic: str, gdelt_velocity: dict) -> float:
    """
    Convert GDELT velocity signal into an inside-view probability adjustment.
    Strong acceleration = +10-15% nudge towards the event happening.
    """
    if gdelt_velocity.get("signal_strength") == "strong":
        return 0.15   # +15% nudge
    elif gdelt_velocity.get("signal_strength") == "moderate":
        return 0.10
    else:
        return 0.0


# ─── STEP 3 — Extremisation ───────────────────────────────────────────────────

def _extremise(outside: float, inside_adjustment: float,
               outside_weight: float = 0.70) -> float:
    """
    Blend outside view and inside view.
    inside_view = outside + adjustment (clamped 0-1).
    Result = outside_weight * outside + (1 - outside_weight) * inside_view.
    """
    inside_view = max(0.01, min(0.99, outside + inside_adjustment))
    blended = outside_weight * outside + (1 - outside_weight) * inside_view
    return round(max(0.01, min(0.99, blended)), 3)


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_superforecasting(topic: str, market_yes_price: float) -> dict:
    """
    Full superforecasting pipeline for a topic.
    Returns a rich dict + a formatted Agent 0 text block for the LLM prompt.
    """
    keywords = [w for w in topic.lower().split() if len(w) > 3][:5]

    # Step 1 — Outside view
    outside_data = _get_metaculus_community_predictions(keywords)
    outside_rate = outside_data.get("base_rate", 0.5)
    outside_n    = outside_data.get("n", 0)
    outside_conf = outside_data.get("confidence", "low")

    # Step 2 — Inside view (GDELT velocity)
    gdelt_velocity = _get_gdelt_timeline_volume(topic)
    adjustment = _news_inside_view(topic, gdelt_velocity)

    # Step 3 — Extremisation
    final_prob = _extremise(outside_rate, adjustment)

    # Edge vs market price
    market_edge = round((final_prob - market_yes_price) * 100, 1)

    # Format Agent 0 block for LLM injection
    accel_str = (
        f"⚡ GDELT ACCELERATION détectée (z={gdelt_velocity.get('z_score', 0):.1f}σ) — "
        f"signal {gdelt_velocity['signal_strength'].upper()}"
        if gdelt_velocity.get("acceleration")
        else f"Pas d'accélération médiatique (z={gdelt_velocity.get('z_score', 0):.1f}σ)"
    )

    agent0_block = (
        f"[AGENT 0 — SUPERFORECASTING]\n"
        f"Outside view (Metaculus, n={outside_n}, confiance={outside_conf}): {outside_rate:.0%}\n"
        f"Inside view (GDELT): {accel_str}\n"
        f"Ajustement inside view: {adjustment:+.0%}\n"
        f"Proba calibrée finale (70% outside + 30% inside): {final_prob:.0%}\n"
        f"Prix marché Polymarket: {market_yes_price:.0%}\n"
        f"Edge superforecaster: {market_edge:+.1f}pts\n"
    )

    logger.info("[SF] topic=%r outside=%.0f%% adj=%+.0f%% final=%.0f%% edge=%+.1fpts",
                topic, outside_rate * 100, adjustment * 100, final_prob * 100, market_edge)

    return {
        "outside_rate": outside_rate,
        "outside_n": outside_n,
        "outside_confidence": outside_conf,
        "gdelt_velocity": gdelt_velocity,
        "inside_adjustment": adjustment,
        "final_prob": final_prob,
        "market_edge": market_edge,
        "agent0_block": agent0_block,
    }


def format_superforecasting_summary(sf: dict) -> str:
    """Short Telegram-friendly summary of superforecasting output."""
    if not sf:
        return ""
    gv = sf.get("gdelt_velocity", {})
    accel = "⚡ ACCÉLÉRATION" if gv.get("acceleration") else "📉 stable"
    return (
        f"🧠 *SUPERFORECASTING — Agent 0*\n\n"
        f"Outside view (Metaculus, n={sf['outside_n']}): *{sf['outside_rate']:.0%}*\n"
        f"News GDELT: {accel} (z={gv.get('z_score', 0):.1f}σ)\n"
        f"Proba calibrée: *{sf['final_prob']:.0%}*\n"
        f"Edge vs marché: *{sf['market_edge']:+.1f}pts*"
    )
