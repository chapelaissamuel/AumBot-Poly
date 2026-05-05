#!/usr/bin/env python3
"""
AUM NEXUS POLY — TOP 1% Prediction Market Bot
Commands: /poly /poly day /poly future /sports /arb /xarb
          /whales /track /calibration /risk
"""

import os
import re
import asyncio
import logging
import time
from datetime import datetime

from telegram import Update, Bot
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
from tracker import (
    init_db, save_prediction, resolve_prediction, get_stats,
    get_portfolio_status, check_risk_before_trade,
    get_calibration_report, apply_calibration,
)
from sports import get_value_bets, OddsApiError
from arb import fetch_arb_opportunities
from data_sources import enrich_future_context
from whale_tracker import get_recent_moves, set_alert_callback, start_background_polling
from kalshi import find_cross_arb
from clob_pressure import get_pressure_for_market
from superforecaster import run_superforecasting, format_superforecasting_summary

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_IDS_ENV = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS: set[int] = set()
if ALLOWED_IDS_ENV.strip():
    for _x in ALLOWED_IDS_ENV.split(","):
        _x = _x.strip()
        if _x.isdigit():
            ALLOWED_IDS.add(int(_x))

# Chat IDs to receive whale alerts (auto-populated on /start)
_alert_chat_ids: set[int] = set()
_bot_instance: Bot | None = None


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_IDS:
        return True
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


async def _broadcast(msg: str):
    """Send a message to all registered alert chat IDs (whale alerts etc.)."""
    if not _bot_instance or not _alert_chat_ids:
        return
    for cid in list(_alert_chat_ids):
        try:
            await _bot_instance.send_message(
                chat_id=cid, text=msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("[BROADCAST] chat_id=%s error: %s", cid, exc)


# ─── Whale alert callback (called from background thread) ─────────────────────
async def _whale_alert(msg: str):
    await _broadcast(msg)


# ─── /start ───────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        _alert_chat_ids.add(update.effective_chat.id)
    await safe_reply(
        update,
        "🎯 *AUM NEXUS POLY* — TOP 1% Prediction Intelligence\n\n"
        "*Polymarket*\n"
        "📊 /poly — MAICR \\+ CLOB pressure \\+ Bull/Bear\n"
        "📊 /poly day — Marchés fermant dans 24h\n"
        "🔭 /poly future \\<topic\\> — Crystal Ball 6\\-agents \\+ Superforecasting\n\n"
        "*Sports*\n"
        "⚽ /sports — Value bets EPL \\+ NBA avec form réelle\n\n"
        "*Arbitrage*\n"
        "💰 /arb — Arbitrage intra\\-Polymarket\n"
        "🔀 /xarb — Arbitrage cross Polymarket \\/ Kalshi\n\n"
        "*Smart Money*\n"
        "🐋 /whales — Derniers moves des wallets profitables\n\n"
        "*Paper Trading*\n"
        "📈 /track — Journal \\+ ROI\n"
        "📈 /track resolve \\<id\\> \\<1\\|0\\>\n\n"
        "*Analytics*\n"
        "🧠 /calibration — Brier Score \\+ biais par bucket\n"
        "⚠️ /risk — Portfolio exposure \\+ drawdown\n\n"
        "_LLM: Gemini 2\\.5 Flash → Groq Llama 3\\.3 → OpenRouter_\n"
        "_Alertes whale activées pour ce chat_ 🐋",
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

    # Fetch CLOB order book pressure for top market (parallel with scores display)
    clob_task = asyncio.to_thread(get_pressure_for_market, scored[0]) if scored else None

    try:
        await safe_reply(update, format_scores_message(scored), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, format_scores_message(scored))

    # Show CLOB pressure signal
    if clob_task:
        try:
            pressure = await clob_task
            if pressure and pressure.get("label"):
                await safe_reply(update, pressure["label"])
        except Exception as exc:
            logger.warning("[POLY] clob pressure error: %s", exc)

    await safe_reply(update, "🤖 Analyse Bull vs Bear en cours…")
    llm_context = build_bull_bear_context(scored)
    analysis_raw = await asyncio.to_thread(llm_call, llm_context, 1200)
    logger.info("[POLY] LLM raw (%d chars): %s…", len(analysis_raw), analysis_raw[:200])

    analysis = format_bull_bear_with_verdict(analysis_raw, scored)

    _PROBA_RE = re.compile(
        r"PROBA\s+VRAIE\s+ESTIM[ÉE]+\s*:?\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE
    )
    proba_matches = _PROBA_RE.findall(analysis_raw)
    logger.info("[POLY] proba matches: %s", proba_matches)

    verdict_blocks = []
    for idx, m in enumerate(scored):
        try:
            if idx < len(proba_matches):
                raw_prob = max(0.01, min(0.99, float(proba_matches[idx]) / 100))
            else:
                raw_prob = m["yes_float"]
            true_prob = apply_calibration(raw_prob)
            v = calculate_net_verdict(m["yes_float"], true_prob)

            # Risk check before saving
            allowed, reason = check_risk_before_trade(v["kelly_pct"], "polymarket")
            risk_note = f"\n⚠️ {reason}" if not allowed else ""

            verdict_blocks.append(
                f"\n📐 *#{idx+1} — {m['question'][:50]}…*\n"
                f"PROBA VRAIE: {raw_prob:.0%} → calibrée: {true_prob:.0%}\n"
                f"⚖️ NET: {v['net_pts']:+.1f}pts — {v['verdict']} ({v['certitude']})\n"
                f"👉 {v['recommandation']}\n"
                f"💰 KELLY: {v['kelly_pct']*100:.1f}% bankroll{risk_note}"
            )
            if allowed:
                try:
                    save_prediction(
                        market=m["question"],
                        yes_price=m["yes_float"],
                        true_prob=true_prob,
                        verdict=v["verdict"],
                        kelly_pct=v["kelly_pct"],
                        category="polymarket",
                    )
                except Exception as exc:
                    logger.warning("[POLY] save_prediction failed: %s", exc)
        except Exception as exc:
            logger.warning("[POLY] verdict block %d failed: %s", idx, exc)

    try:
        await safe_reply(update, f"📈 *Analyse MAICR*\n\n{analysis}", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, f"📈 Analyse MAICR\n\n{analysis}")

    if verdict_blocks:
        verdict_msg = "⚖️ *VERDICTS NET — Python PUR*\n" + "\n".join(verdict_blocks)
        try:
            await safe_reply(update, verdict_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await safe_reply(update, verdict_msg)


# ─── /poly day ────────────────────────────────────────────────────────────────
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
        await safe_reply(update, "📭 Aucun marché ne ferme dans les 24h.\nEssaie /poly pour les marchés globaux.")
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
        await safe_reply(update, f"📈 *Analyse MAICR J-24h*\n\n{analysis}", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, f"📈 Analyse MAICR J-24h\n\n{analysis}")


# ─── /poly future ─────────────────────────────────────────────────────────────
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

    yes_float = live_market.get("yes_float", live_market["yes_odds"] / 100)

    # Run superforecasting + data enrichment in parallel
    await safe_reply(update, "🧠 Superforecasting + base rates + GDELT…")
    sf_task      = asyncio.to_thread(run_superforecasting, topic, yes_float)
    enrich_task  = asyncio.to_thread(enrich_future_context, topic)
    sf_data, enriched = await asyncio.gather(sf_task, enrich_task)

    logger.info("[FUTURE] SF final_prob=%.0f%% edge=%+.1fpts",
                sf_data.get("final_prob", 0) * 100, sf_data.get("market_edge", 0))

    # Show superforecasting summary
    sf_summary = format_superforecasting_summary(sf_data)
    if sf_summary:
        try:
            await safe_reply(update, sf_summary, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await safe_reply(update, sf_summary)

    # Show enriched data summary
    if enriched:
        summary_lines = []
        for line in enriched.splitlines():
            if line.strip() and not line.startswith("  Exemples"):
                summary_lines.append(line.strip())
            if len(summary_lines) >= 6:
                break
        if summary_lines:
            try:
                await safe_reply(
                    update,
                    "📊 *Données externes chargées*\n\n" + "\n".join(f"• {l}" for l in summary_lines),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                await safe_reply(update, "📊 Données:\n" + "\n".join(summary_lines))

    # Build LLM context with Agent 0 superforecasting block prepended
    agent0_block = sf_data.get("agent0_block", "")
    full_enriched = (agent0_block + "\n\n" + enriched).strip() if agent0_block else enriched
    market_ctx = build_future_llm_context(live_market, full_enriched)

    await safe_reply(update, "🤖 Analyse Crystal Ball en cours…")
    analysis = await asyncio.to_thread(llm_call, market_ctx, 900)

    # NET verdict — prefer superforecaster calibrated prob, then LLM
    try:
        sf_prob = sf_data.get("final_prob")
        match = re.search(r"PROBABILITÉ VRAIE\s*[:\(]\s*(\d+(?:\.\d+)?)\s*%", analysis)
        if not match:
            match = re.search(r"(\d{2,3})\s*%", analysis)
        llm_prob = float(match.group(1)) / 100 if match else None

        # Blend: 60% superforecaster + 40% LLM if both available
        if sf_prob and llm_prob:
            true_prob = round(0.60 * sf_prob + 0.40 * llm_prob, 3)
        else:
            true_prob = sf_prob or llm_prob or yes_float

        true_prob = apply_calibration(max(0.01, min(0.99, true_prob)))
        verdict_data = calculate_net_verdict(yes_float, true_prob)
        net_block = (
            f"\n⚖️ NET: {verdict_data['net_pts']:+.1f}pts — "
            f"{verdict_data['verdict']} ({verdict_data['certitude']})\n"
            f"👉 {verdict_data['recommandation']}\n"
            f"💰 KELLY: {verdict_data['kelly_pct']*100:.1f}% bankroll\n"
            f"🧠 Proba calibrée finale: {true_prob:.0%} "
            f"(SF: {sf_prob:.0%}, LLM: {llm_prob:.0%})" if (sf_prob and llm_prob)
            else f"\n⚖️ NET: {verdict_data['net_pts']:+.1f}pts — "
                 f"{verdict_data['verdict']} | KELLY: {verdict_data['kelly_pct']*100:.1f}%"
        )
        analysis += net_block
    except Exception as exc:
        logger.warning("[FUTURE] net verdict error: %s", exc)

    try:
        await safe_reply(update, f"*🔮 Crystal Ball — {topic}*\n\n{analysis}", parse_mode=ParseMode.MARKDOWN)
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
            logger.warning("[SPORTS] ODDS_API_KEY not set")

        if not key:
            await _send("❌ ODDS_API_KEY non configurée.")
            return

        await _send("⏳ Fetch cotes live EPL + NBA + form réelle…")

        try:
            bets = await asyncio.wait_for(
                asyncio.to_thread(get_value_bets),
                timeout=60,
            )
        except asyncio.TimeoutError:
            await _send("❌ Timeout — réessaie dans quelques minutes.")
            return

        if not bets:
            await _send("📭 Aucun value bet trouvé.\nLes marchés sont peut-être fermés.")
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
        logger.warning("[SPORTS] OddsApiError kind=%s: %s", exc.kind, exc)
        if exc.kind in ("auth", "quota"):
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


# ─── /xarb ────────────────────────────────────────────────────────────────────
async def xarb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_allowed(update.effective_user.id):
        return

    await safe_reply(update, "🔀 Scanner arbitrage cross Polymarket / Kalshi…")

    try:
        divergences = await asyncio.wait_for(
            asyncio.to_thread(find_cross_arb, 5.0),
            timeout=30,
        )
    except asyncio.TimeoutError:
        await safe_reply(update, "❌ Timeout Kalshi/Polymarket — réessaie.")
        return
    except Exception as exc:
        logger.error("[XARB] error: %s", exc, exc_info=True)
        await safe_reply(update, f"❌ Erreur: {exc}")
        return

    if not divergences:
        await safe_reply(
            update,
            "✅ *Aucun arbitrage cross-platform >5pts détecté*\n\n"
            "_Polymarket et Kalshi sont actuellement alignés._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = ["🔀 *CROSS-ARB — Polymarket vs Kalshi*\n"]
    for rank, d in enumerate(divergences[:8], 1):
        sign = "📈" if d["diff_pts"] > 0 else "📉"
        lines.append(
            f"*#{rank}* {sign} Écart: *{abs(d['diff_pts']):.1f}pts*\n"
            f"Kalshi:  {d['kalshi_title'][:55]} → *{d['kalshi_price']:.0%}*\n"
            f"Poly:    {d['poly_question'][:55]} → *{d['poly_price']:.0%}*\n"
            f"⚡ Action: {d['action']}\n"
            f"🔗 [Kalshi]({d['kalshi_url']}) | [Poly]({d['poly_url']})\n"
        )

    try:
        await safe_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, "\n".join(lines))


# ─── /whales ──────────────────────────────────────────────────────────────────
async def whales_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_allowed(update.effective_user.id):
        return

    # Register chat for live alerts
    _alert_chat_ids.add(update.effective_chat.id)

    moves = get_recent_moves(5)

    if not moves:
        await safe_reply(
            update,
            "🐋 *WHALE TRACKER*\n\n"
            "Aucun move smart money détecté récemment.\n"
            "_Le tracker surveille les wallets (win rate >60%, profit >$1k/mois)._\n"
            "_Alertes Telegram automatiques sur les gros trades YES >$5k à <50%._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = ["🐋 *WHALE TRACKER — Derniers moves smart money*\n"]
    for rank, m in enumerate(moves, 1):
        side_emoji = "🟢" if m["side"] in ("BUY", "YES") else "🔴"
        lines.append(
            f"*#{rank}* {side_emoji} {m['ts']}\n"
            f"Wallet: `{m['wallet']}…`\n"
            f"Win rate: {m['win_rate']:.0%} | Profit 30j: ${m['profit_30d']:,.0f}\n"
            f"Marché: _{m['question'][:60]}_\n"
            f"Side: *{m['side']}* | Montant: *${m['amount']:,.0f}*\n"
            f"Prix: {m['price']:.0%}\n"
        )

    lines.append("_🔔 Alertes activées — tu recevras les moves >$5k en temps réel._")

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
        brier = result.get("brier", 0)
        emoji = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➡️")
        await safe_reply(
            update,
            f"{emoji} Prédiction #{pred_id} résolue.\n"
            f"Outcome: {'YES' if outcome == 1 else 'NO'}\n"
            f"PnL: *{pnl*100:+.2f}%* bankroll\n"
            f"Brier score: {brier:.4f} _(0=parfait, 1=pire)_\n"
            f"_Calibration mise à jour automatiquement._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

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
        brier_str = (
            f" | Brier: {row['brier_score']:.3f}"
            if row.get("brier_score") is not None
            else ""
        )
        date_str = (row["timestamp"] or "")[:10]
        short_mkt = row["market"][:38] + "…" if len(row["market"]) > 38 else row["market"]
        lines.append(
            f"{status} *#{row['id']}* [{date_str}] {short_mkt}\n"
            f"   YES: {row['yes_price']:.0%} → Vraie: {row['true_prob']:.0%} | "
            f"{row['verdict']} | Kelly: {row['kelly_pct']*100:.1f}%{pnl_str}{brier_str}"
        )

    lines.append("\n_Résoudre: /track resolve <id> <1|0>_")

    try:
        await safe_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, "\n".join(lines))


# ─── /calibration ─────────────────────────────────────────────────────────────
async def calibration_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_allowed(update.effective_user.id):
        return

    report = await asyncio.to_thread(get_calibration_report)

    lines = ["🧠 *CALIBRATION — Brier Score & Biais LLM*\n"]
    lines.append(
        f"Brier Score global: *{report['global_brier']:.4f}*\n"
        f"_(0.00=parfait | 0.25=aléatoire | 1.00=pire)_\n"
        f"Basé sur *{report['n_resolved']}* prédictions résolues.\n"
    )
    lines.append("*Par bucket de probabilité:*")

    for b in report["buckets"]:
        n = b["n"]
        if n == 0:
            status = "─ aucune donnée"
        else:
            predicted = b["predicted_pct"]
            actual = b["actual_pct"]
            diff = actual - predicted if actual is not None else 0
            if abs(diff) < 3:
                bias_label = "✅ bien calibré"
            elif diff > 0:
                bias_label = f"📈 sous-estimait ({diff:+.0f}%)"
            else:
                bias_label = f"📉 surestimait ({diff:+.0f}%)"
            factor = b["bias_factor"]
            brier_str = f" | Brier: {b['avg_brier']:.3f}" if b["avg_brier"] is not None else ""
            status = f"n={n} | prédit {predicted}% → réel {actual}% | {bias_label} | facteur {factor:.2f}{brier_str}"

        lines.append(f"  *[{b['bucket']}%]* {status}")

    lines.append(
        "\n_Le facteur de biais est appliqué automatiquement à chaque prédiction._\n"
        "_Résous plus de prédictions avec /track resolve pour améliorer la calibration._"
    )

    try:
        await safe_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, "\n".join(lines))


# ─── /risk ────────────────────────────────────────────────────────────────────
async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_allowed(update.effective_user.id):
        return

    status = await asyncio.to_thread(get_portfolio_status)

    halt_emoji = "🛑" if status["trading_halted"] else "✅"
    dd_pct = status["drawdown"] * 100
    bankroll_pct = status["bankroll"] * 100
    exp_pct = status["total_exposure"] * 100

    lines = [
        f"⚠️ *PORTFOLIO RISK MANAGER*\n",
        f"Bankroll: *{bankroll_pct:.1f}%* | Peak: *{status['peak']*100:.1f}%*",
        f"Drawdown depuis pic: *{dd_pct:.1f}%* {'🛑' if dd_pct >= 15 else '✅'}",
        f"Exposure totale: *{exp_pct:.1f}%* / 25% max {'⚠️' if exp_pct >= 20 else '✅'}",
        f"Positions actives: *{status['active_positions']}*",
        f"Statut: {halt_emoji} *{'TRADING SUSPENDU' if status['trading_halted'] else 'Actif'}*\n",
    ]

    if status["category_exposure"]:
        lines.append("*Exposure par catégorie:*")
        for cat, exp in status["category_exposure"].items():
            bar = "█" * int(exp * 100 / 2)
            over = " ⚠️" if exp > 0.10 else ""
            lines.append(f"  {cat.upper()}: {exp*100:.1f}% {bar}{over}")

    if status["flags"]:
        lines.append("\n*⚠️ Alertes actives:*")
        for flag in status["flags"]:
            lines.append(f"  {flag}")

    lines.append(
        "\n_Limites: 25% total | 10% par catégorie | halt à -15% drawdown_\n"
        "_Résous des positions avec /track resolve pour libérer de l'exposure._"
    )

    try:
        await safe_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await safe_reply(update, "\n".join(lines))


# ─── /help ────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


# ─── Post-init ────────────────────────────────────────────────────────────────
async def post_init(application: Application):
    global _bot_instance
    _bot_instance = application.bot
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("[INIT] Webhook supprimé — token libéré")
    except Exception as exc:
        logger.warning("[INIT] delete_webhook failed: %s", exc)
    application.bot_data["start_time"] = time.time()

    # Register whale alert callback
    set_alert_callback(_whale_alert)
    # Start background whale polling
    start_background_polling()
    logger.info("[INIT] Whale tracker background thread started")


# ─── Error handler ────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set. Add it to Secrets.")

    init_db()
    logger.info("[BOOT] predictions.db initialised")
    logger.info("[BOOT] AUM NEXUS POLY TOP 1% starting…")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",       start_command))
    app.add_handler(CommandHandler("help",        help_command))
    app.add_handler(CommandHandler("poly",        poly_command))
    app.add_handler(CommandHandler("sports",      sports_command))
    app.add_handler(CommandHandler("arb",         arb_command))
    app.add_handler(CommandHandler("xarb",        xarb_command))
    app.add_handler(CommandHandler("whales",      whales_command))
    app.add_handler(CommandHandler("track",       track_command))
    app.add_handler(CommandHandler("calibration", calibration_command))
    app.add_handler(CommandHandler("risk",        risk_command))
    app.add_error_handler(error_handler)

    logger.info("[BOOT] All 10 handlers registered. Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
