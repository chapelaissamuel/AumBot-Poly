#!/usr/bin/env python3
"""
AUM NEXUS POLY — Production Telegram Prediction Market Bot
Commands: /poly, /poly day, /poly future <topic>, /sports, /arb, /track
"""

import os
import re
import asyncio
import logging
import time
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut, RetryAfter
from telegram.ext import (
    Application, CommandHandler, ContextTypes, filters
)

from polymarket_maicr import (
    fetch_markets, fetch_markets_day, maicr_score,
    format_scores_message, format_day_scores_message,
    enrich_with_hours, build_bull_bear_context,
    format_bull_bear_with_verdict, fetch_live_polymarket_odds,
    build_future_llm_context, calculate_net_verdict,
    MARKET_NOT_FOUND_MSG,
)
from llm import llm_call
from tracker import init_db, save_prediction, resolve_prediction, get_stats
from sports import get_value_bets, OddsApiError
from arb import fetch_arb_opportunities

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Comma-separated list of allowed Telegram user IDs (leave empty to allow all)
ALLOWED_IDS_ENV = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS: set[int] = set()
if ALLOWED_IDS_ENV.strip():
    for x in ALLOWED_IDS_ENV.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_IDS.add(int(x))


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_IDS:
        return True  # open to all if not configured
    return user_id in ALLOWED_IDS


# ─── Telegram helpers ─────────────────────────────────────────────────────────
def split_message(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def safe_reply(update: Update, text: str, parse_mode=None):
    try:
        for chunk in split_message(text):
            await update.message.reply_text(chunk, parse_mode=parse_mode,
                                            disable_web_page_preview=True)
    except BadRequest:
        if parse_mode:
            try:
                for chunk in split_message(text):
                    await update.message.reply_text(chunk, disable_web_page_preview=True)
            except Exception as exc:
                logger.error("safe_reply fallback failed: %s", exc)
    except RetryAfter as exc:
        logger.warning("RetryAfter %ss", exc.retry_after)
        await asyncio.sleep(exc.retry_after)
        await update.message.reply_text(text, disable_web_page_preview=True)
    except (NetworkError, TimedOut) as exc:
        logger.warning("Network error: %s", exc)
    except Exception as exc:
        logger.error("safe_reply failed: %s", exc)


# ─── /start ───────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update,
        "🎯 *AUM NEXUS POLY* — Prediction Market Intelligence\n\n"
        "*Polymarket*\n"
        "📊 /poly — Top 3 marchés par score MAICR + analyse Bull/Bear\n"
        "📊 /poly day — Marchés fermant dans 24h\n"
        "🔭 /poly future \\<topic\\> — Crystal Ball sur un sujet précis\n\n"
        "*Sports*\n"
        "⚽ /sports — Value bets EPL \\+ NBA \\(The Odds API\\)\n\n"
        "*Arbitrage*\n"
        "💰 /arb — Scanner d'arbitrage Polymarket\n\n"
        "*Paper Trading*\n"
        "📈 /track — Journal de paper trading \\+ ROI\n"
        "📈 /track resolve \\<id\\> \\<1\\|0\\> — Résoudre une prédiction\n\n"
        "_LLM stack: Gemini 2\\.5 Flash → Groq Llama 3\\.3 → OpenRouter_",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ─── /poly (main + dispatch) ──────────────────────────────────────────────────
async def poly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_allowed(update.effective_user.id):
        await safe_reply(update, f"⛔ Accès refusé. Ton ID: {update.effective_user.id if update.effective_user else '?'}")
        return

    first = (context.args[0].lower() if context.args else "")

    if first == "day":
        await poly_day_command(update, context)
        return

    if first == "future":
        await poly_future_command(update, context)
        return

    await safe_reply(update, "⏳ Fetch Polymarket + calcul MAICR…")

    markets = await asyncio.to_thread(fetch_markets)
    if not markets:
        await safe_reply(update, "❌ API Polymarket inaccessible. Réessaie dans quelques secondes.")
        return

    scored = sorted(
        [maicr_score(m) for m in markets],
        key=lambda x: x["score"],
        reverse=True,
    )[:3]

    try:
        await safe_reply(update, format_scores_message(scored), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, format_scores_message(scored))

    await safe_reply(update, "🤖 Analyse Bull vs Bear en cours…")
    llm_context = build_bull_bear_context(scored)
    analysis_raw = await asyncio.to_thread(llm_call, llm_context, 1200)
    logger.info("[POLY] LLM raw (%d chars): %s…", len(analysis_raw), analysis_raw[:200])

    # Post-process: let format_bull_bear_with_verdict inject NET blocks
    analysis = format_bull_bear_with_verdict(analysis_raw, scored)

    # Extract ALL "PROBA VRAIE ESTIMÉE: X%" from raw output (one per market)
    _PROBA_RE = re.compile(
        r"PROBA\s+VRAIE\s+ESTIM[ÉE]+\s*:?\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE
    )
    proba_matches = _PROBA_RE.findall(analysis_raw)
    logger.info("[POLY] proba matches found: %s", proba_matches)

    # Build per-market verdict blocks and save to DB
    verdict_blocks = []
    for idx, m in enumerate(scored):
        try:
            if idx < len(proba_matches):
                true_prob = max(0.01, min(0.99, float(proba_matches[idx]) / 100))
            else:
                true_prob = m["yes_float"]  # fallback: market price
            v = calculate_net_verdict(m["yes_float"], true_prob)
            verdict_blocks.append(
                f"\n📐 *#{idx+1} — {m['question'][:50]}…*\n"
                f"PROBA VRAIE: {true_prob:.0%}\n"
                f"⚖️ NET: {v['net_pts']:+.1f}pts — {v['verdict']} ({v['certitude']})\n"
                f"👉 {v['recommandation']}\n"
                f"💰 KELLY: {v['kelly_pct']*100:.1f}% bankroll"
            )
            try:
                save_prediction(
                    market=m["question"],
                    yes_price=m["yes_float"],
                    true_prob=true_prob,
                    verdict=v["verdict"],
                    kelly_pct=v["kelly_pct"],
                )
            except Exception as exc:
                logger.warning("[POLY] save_prediction failed: %s", exc)
        except Exception as exc:
            logger.warning("[POLY] verdict block %d failed: %s", idx, exc)

    # Send LLM analysis
    try:
        await safe_reply(update, f"📈 *Analyse MAICR*\n\n{analysis}", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, f"📈 Analyse MAICR\n\n{analysis}")

    # Always send verdict summary — never silently omit
    if verdict_blocks:
        verdict_msg = "⚖️ *VERDICTS NET — Python PUR*\n" + "\n".join(verdict_blocks)
        try:
            await safe_reply(update, verdict_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await safe_reply(update, verdict_msg)


async def poly_day_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update,
        "⚠️ *ATTENTION — Marchés J-24h*\n\n"
        "Ces marchés ferment dans moins de 24h.\n"
        "• Spreads élevés — frais taker max\n"
        "• Peu de temps pour corriger une position\n"
        "• PAPER TRADING uniquement\n\n"
        "Utilise /poly pour les marchés optimaux \\(J-7 à J-60\\).",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await safe_reply(update, "⏳ Fetch marchés J-24h + calcul MAICR…")

    markets = await asyncio.to_thread(fetch_markets_day)
    if not markets:
        await safe_reply(
            update,
            "📭 Aucun marché ne ferme dans les 24h.\nEssaie /poly pour les marchés globaux.",
        )
        return

    scored = sorted(
        [maicr_score(m) for m in markets],
        key=lambda x: x["score"],
        reverse=True,
    )[:3]
    scored = enrich_with_hours(scored, markets)

    try:
        await safe_reply(update, format_day_scores_message(scored), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, format_day_scores_message(scored))

    await safe_reply(update, "🤖 Analyse Bull vs Bear J-24h en cours…")
    ctx = build_bull_bear_context(scored)
    analysis_raw = await asyncio.to_thread(llm_call, ctx, 1200)
    analysis = format_bull_bear_with_verdict(analysis_raw, scored)

    try:
        await safe_reply(
            update, f"📈 *Analyse MAICR J-24h*\n\n{analysis}", parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        await safe_reply(update, f"📈 Analyse MAICR J-24h\n\n{analysis}")


async def poly_future_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args or [])
    if args and args[0].lower() == "future":
        args = args[1:]

    if not args:
        await safe_reply(
            update,
            "Usage: /poly future <sujet>\n"
            "Ex: /poly future Taiwan invasion\n"
            "Ex: /poly future Fed rate cut",
        )
        return

    topic = " ".join(args).strip()
    await safe_reply(update, f"🔭 Crystal Ball — {topic}")
    await safe_reply(update, "📊 Recherche marché Polymarket…")

    live_market = await asyncio.to_thread(fetch_live_polymarket_odds, topic)
    if not live_market:
        await safe_reply(
            update,
            f"❌ Marché non trouvé sur Polymarket pour: {topic}\n"
            "Essaie des mots-clés plus précis en anglais.",
        )
        return

    market_msg = (
        f"📈 *Marché trouvé*\n\n"
        f"*{live_market['question']}*\n\n"
        f"YES: {live_market['yes_odds']}% | NO: {live_market['no_odds']}%\n"
        f"Volume: {live_market['volume']} | Liquidité: {live_market['liquidity']}\n"
        f"Expiration: {live_market['end_date']}\n"
        f"🔗 {live_market['url']}"
    )
    try:
        await safe_reply(update, market_msg, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, market_msg)

    await safe_reply(update, "🤖 Analyse Crystal Ball en cours…")
    market_ctx = build_future_llm_context(live_market)
    analysis = await asyncio.to_thread(llm_call, market_ctx, 800)

    # Calculate NET verdict
    try:
        import re
        match = re.search(r"PROBABILITÉ VRAIE\s*[:\(]\s*(\d+(?:\.\d+)?)\s*%", analysis)
        if not match:
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", analysis)
        if match:
            true_prob = float(match.group(1)) / 100
            yes_float = live_market.get("yes_float", live_market["yes_odds"] / 100)
            verdict_data = calculate_net_verdict(yes_float, true_prob)
            net_block = (
                f"\n⚖️ NET: {verdict_data['net_pts']:+.1f}pts — "
                f"{verdict_data['verdict']} ({verdict_data['certitude']})\n"
                f"👉 {verdict_data['recommandation']}\n"
                f"💰 KELLY: {verdict_data['kelly_pct']*100:.1f}% bankroll"
            )
            analysis += net_block
    except Exception:
        pass

    try:
        await safe_reply(
            update, f"*🔮 Crystal Ball — {topic}*\n\n{analysis}", parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        await safe_reply(update, f"🔮 Crystal Ball — {topic}\n\n{analysis}")


# ─── /sports ──────────────────────────────────────────────────────────────────
async def sports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_allowed(update.effective_user.id):
        return

    responded = False

    async def _send(text: str, md: bool = False):
        nonlocal responded
        responded = True
        if md:
            try:
                await safe_reply(update, text, parse_mode=ParseMode.MARKDOWN)
                return
            except Exception:
                pass
        await safe_reply(update, text)

    try:
        key = os.environ.get("ODDS_API_KEY", "")
        if key:
            logger.info("[SPORTS] ODDS_API_KEY prefix: %s…", key[:4])
        else:
            logger.warning("[SPORTS] ODDS_API_KEY is empty or not set")

        if not key:
            await _send("❌ ODDS_API_KEY non configurée.")
            return

        await _send("⏳ Fetch cotes live EPL + NBA…")

        # Hard 60s wall-clock timeout on the entire blocking call
        # (each sport: 10s HTTP + up to 5×LLM calls)
        try:
            bets = await asyncio.wait_for(
                asyncio.to_thread(get_value_bets),
                timeout=60,
            )
        except asyncio.TimeoutError:
            await _send("❌ The Odds API timeout — réessaie dans quelques minutes.")
            return

        if not bets:
            await _send(
                "📭 Aucun value bet trouvé.\n"
                "Les marchés sont peut-être fermés ou l'API ne retourne aucune cote."
            )
            return

        lines = ["⚽🏀 *AUM NEXUS — SPORTS VALUE BETS*\n"]
        for rank, g in enumerate(bets, 1):
            direction = "HOME" if g["edge"] > 0 else "AWAY"
            target = g["home"] if g["edge"] > 0 else g["away"]
            lines.append(
                f"*#{rank} — {g['sport']}*\n"
                f"🏟️ {g['away']} @ {g['home']}\n"
                f"🕐 {g['commence']}\n"
                f"Implied: {g['imp_home']:.0%} → Vraie: {g['true_home']:.0%}\n"
                f"⚖️ Edge: {g['edge']:+.1f}pts → BET *{direction}* ({target})\n"
                f"Cote: {g['odds_home']:.2f} | KELLY: {g['kelly']*100:.1f}%\n"
                f"💬 _{g['reasoning']}_\n"
            )
        await _send("\n".join(lines), md=True)

    except OddsApiError as exc:
        logger.warning("[SPORTS] OddsApiError: %s", exc)
        if exc.kind in ("auth",):
            await _send("❌ ODDS_API_KEY invalide ou quota épuisé")
        elif exc.kind == "quota":
            await _send("❌ ODDS_API_KEY invalide ou quota épuisé")
        elif exc.kind == "timeout":
            await _send("❌ The Odds API timeout — réessaie")
        else:
            await _send(f"❌ Erreur Odds API: {exc}")

    except Exception as exc:
        logger.error("[SPORTS] unexpected error: %s", exc, exc_info=True)
        await _send(f"❌ Erreur inattendue: {exc}")

    finally:
        if not responded:
            await safe_reply(update, "❌ /sports n'a pas pu répondre — réessaie.")


# ─── /arb ─────────────────────────────────────────────────────────────────────
async def arb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_allowed(update.effective_user.id):
        return

    await safe_reply(update, "⏳ Scanner arbitrage Polymarket…")

    opps = await asyncio.to_thread(fetch_arb_opportunities)

    if not opps:
        await safe_reply(
            update,
            "✅ *Aucun arbitrage détecté*\n\n"
            "_Les marchés Polymarket sont actuellement bien pricés \\(sum YES ≤ 1\\.02\\)\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    lines = ["💰 *AUM NEXUS — ARB SCANNER*\n"]
    for rank, arb in enumerate(opps[:5], 1):
        prices_str = " + ".join(f"{p:.2%}" for p in arb["yes_prices"])
        vol_str = f"${arb['volume']/1000:.0f}k" if arb["volume"] > 0 else "N/A"
        lines.append(
            f"*#{rank}* — {arb['title']}\n"
            f"YES sum: {prices_str} = *{arb['sum_yes']:.4f}*\n"
            f"🎯 Edge: *{arb['edge_cents']:.1f}¢* par dollar | Vol: {vol_str}\n"
            f"🔗 {arb['url']}\n"
        )

    try:
        await safe_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, "\n".join(lines))


# ─── /track ───────────────────────────────────────────────────────────────────
async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_allowed(update.effective_user.id):
        return

    args = context.args or []

    if args and args[0].lower() == "resolve":
        if len(args) < 3:
            await safe_reply(update, "Usage: /track resolve <id> <1|0>\n1=YES a gagné, 0=NO a gagné")
            return
        try:
            pred_id = int(args[1])
            outcome = int(args[2])
            if outcome not in (0, 1):
                raise ValueError
        except ValueError:
            await safe_reply(update, "❌ Usage: /track resolve <id> <1|0>")
            return

        result = await asyncio.to_thread(resolve_prediction, pred_id, outcome)
        if "error" in result:
            await safe_reply(update, f"❌ {result['error']}")
            return

        pnl = result["pnl"]
        emoji = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➡️")
        await safe_reply(
            update,
            f"{emoji} Prédiction #{pred_id} résolue.\n"
            f"Outcome: {'YES' if outcome == 1 else 'NO'}\n"
            f"PnL: *{pnl*100:+.2f}%* bankroll",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Default: show stats + recent predictions
    stats = await asyncio.to_thread(get_stats)
    roi = stats["total_pnl"] * 100
    win_rate = stats["win_rate"]

    lines = [
        "📈 *AUM NEXUS — PAPER TRADING*\n",
        f"Total: *{stats['total']}* | Résolus: *{stats['resolved']}*",
        f"Wins: *{stats['wins']}* | Win rate: *{win_rate:.1f}%*",
        f"ROI cumulé: *{roi:+.2f}%* bankroll\n",
        "*10 dernières prédictions:*",
    ]

    for row in stats["recent"]:
        status = "⏳" if not row["resolved"] else ("✅" if (row["pnl"] or 0) > 0 else "❌")
        pnl_str = (
            f" | PnL: {row['pnl']*100:+.2f}%"
            if row["resolved"] and row["pnl"] is not None
            else ""
        )
        date_str = (row["timestamp"] or "")[:10]
        short_mkt = row["market"][:38] + "…" if len(row["market"]) > 38 else row["market"]
        lines.append(
            f"{status} *#{row['id']}* [{date_str}] {short_mkt}\n"
            f"   YES: {row['yes_price']:.0%} → Vraie: {row['true_prob']:.0%} | "
            f"{row['verdict']} | Kelly: {row['kelly_pct']*100:.1f}%{pnl_str}"
        )

    lines.append("\n_Résoudre: /track resolve <id> <1|0>_")

    try:
        await safe_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, "\n".join(lines))


# ─── /help ────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


# ─── Post-init (delete webhook to avoid conflicts) ───────────────────────────
async def post_init(application: Application):
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("[INIT] Webhook supprimé — token libéré")
    except Exception as exc:
        logger.warning("[INIT] delete_webhook failed (non-blocking): %s", exc)
    application.bot_data["start_time"] = time.time()


# ─── Error handler ────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set. Add it to Secrets.")

    init_db()
    logger.info("[BOOT] predictions.db initialised")
    logger.info("[BOOT] AUM NEXUS POLY starting…")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("poly", poly_command))
    app.add_handler(CommandHandler("sports", sports_command))
    app.add_handler(CommandHandler("arb", arb_command))
    app.add_handler(CommandHandler("track", track_command))
    app.add_error_handler(error_handler)

    logger.info("[BOOT] All handlers registered. Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
