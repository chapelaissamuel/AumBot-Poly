"""
briefing.py — Daily automated briefing at 08:00 Paris time.

Uses APScheduler to fire daily briefing to all registered Telegram chats.
Briefing includes: top MAICR, top EV bets, last whale move, best parlay, 7-day stats.
"""

import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
from apscheduler.triggers.cron import CronTrigger                  # type: ignore

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_briefing_callback = None   # async fn(msg: str) → sends to all registered chats


def set_briefing_callback(fn):
    global _briefing_callback
    _briefing_callback = fn


def build_briefing_text() -> str:
    """
    Synchronously build the daily briefing text.
    Each section gracefully degrades on error.
    """
    today = datetime.now().strftime("%d %B %Y")
    lines = [f"☀️ *BRIEFING AUM NEXUS — {today}*\n"]

    # ── TOP MAICR ──────────────────────────────────────────────────────────────
    try:
        from polymarket_maicr import fetch_markets, maicr_score, format_scores_message
        markets = fetch_markets()
        if markets:
            scored = sorted(
                [maicr_score(m) for m in markets],
                key=lambda x: x["score"], reverse=True,
            )[:3]
            lines.append("📊 *TOP MARCHÉS POLYMARKET*")
            for i, m in enumerate(scored, 1):
                lines.append(
                    f"#{i} [{m['score']}/100] {m['question'][:55]}\n"
                    f"   YES: {m['yes']} | Vol: {m['volume']}"
                )
        else:
            lines.append("📊 TOP POLYMARKET: API indisponible")
    except Exception as exc:
        logger.warning("[BRIEFING] MAICR error: %s", exc)
        lines.append("📊 TOP POLYMARKET: erreur de fetch")

    lines.append("")

    # ── VALUE BETS SPORT ───────────────────────────────────────────────────────
    try:
        from ev_scanner import scan_positive_ev
        ev_bets = scan_positive_ev(limit_per_sport=2)
        if ev_bets:
            lines.append("⚽ *VALUE BETS SPORT DU JOUR*")
            for bet in ev_bets[:3]:
                lines.append(
                    f"• {bet['match'][:40]} — *{bet['outcome']}*\n"
                    f"  {bet['bookmaker']} @{bet['odds']:.2f} | "
                    f"EV: {bet['ev']:+.1%} | Kelly: {bet['kelly']*100:.1f}%"
                )
        else:
            lines.append("⚽ VALUE BETS: aucun +EV détecté")
    except Exception as exc:
        logger.warning("[BRIEFING] EV error: %s", exc)
        lines.append("⚽ VALUE BETS: indisponible")

    lines.append("")

    # ── DERNIER MOVE WHALE ─────────────────────────────────────────────────────
    try:
        from whale_tracker import get_recent_moves
        moves = get_recent_moves(1)
        if moves:
            m = moves[0]
            lines.append(
                f"🐋 *DERNIER MOVE WHALE*\n"
                f"Wallet: {m['wallet']}… | WR: {m['win_rate']:.0%}\n"
                f"{m['side']} ${m['amount']:,.0f} — _{m['question'][:55]}_\n"
                f"Prix: {m['price']:.0%} | {m['ts']}"
            )
        else:
            lines.append("🐋 WHALE: pas de move récent")
    except Exception as exc:
        logger.warning("[BRIEFING] whale error: %s", exc)
        lines.append("🐋 WHALE: indisponible")

    lines.append("")

    # ── COMBINÉ OPTIMAL ────────────────────────────────────────────────────────
    try:
        from parlay import find_best_parlay
        parlay = find_best_parlay()
        if parlay and parlay.get("combined_ev", 0) > 0.05:
            legs_str = " + ".join(
                f"{l['outcome'][:20]} @{l['odds']:.2f}"
                for l in parlay["legs"]
            )
            lines.append(
                f"💰 *COMBINÉ OPTIMAL*\n"
                f"{legs_str}\n"
                f"Cote: {parlay['combined_odds']:.2f} | "
                f"EV: {parlay['combined_ev']:+.1%} | "
                f"Mise: {parlay['kelly']*100:.1f}% bankroll"
            )
        else:
            lines.append("💰 COMBINÉ: aucun EV >5% en ce moment")
    except Exception as exc:
        logger.warning("[BRIEFING] parlay error: %s", exc)
        lines.append("💰 COMBINÉ: indisponible")

    lines.append("")

    # ── PERFORMANCE 7 JOURS ────────────────────────────────────────────────────
    try:
        from tracker import get_stats, get_calibration_report
        stats  = get_stats()
        calib  = get_calibration_report()
        wr     = stats["win_rate"]
        roi    = stats["total_pnl"] * 100
        brier  = calib["global_brier"]
        lines.append(
            f"📈 *PERFORMANCE 7 JOURS*\n"
            f"Win rate: *{wr:.1f}%* | ROI: *{roi:+.2f}%*\n"
            f"Brier Score: *{brier:.4f}* _(0=parfait)_\n"
            f"Total prédictions: {stats['total']} | Résolues: {stats['resolved']}"
        )
    except Exception as exc:
        logger.warning("[BRIEFING] stats error: %s", exc)
        lines.append("📈 PERFORMANCE: indisponible")

    lines.append("\n_AUM NEXUS POLY — TOP 1% Prediction Intelligence_ 🎯")
    return "\n".join(lines)


def fire_briefing():
    """Called by scheduler at 08:00 Paris. Builds and dispatches briefing."""
    import asyncio
    logger.info("[BRIEFING] firing daily briefing")
    try:
        text = build_briefing_text()
        if _briefing_callback:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_briefing_callback(text))
            loop.close()
    except Exception as exc:
        logger.error("[BRIEFING] error building/sending: %s", exc, exc_info=True)


def start_briefing_scheduler():
    """Start APScheduler — fires daily at 08:00 Europe/Paris."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="Europe/Paris")
    _scheduler.add_job(
        fire_briefing,
        CronTrigger(hour=8, minute=0, timezone="Europe/Paris"),
        id="daily_briefing",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("[BRIEFING] Scheduler started — fires daily at 08:00 Paris")
