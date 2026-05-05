"""
Polymarket MAICR — Score Python PUR + analyse Bull vs Bear via LLM.
Extracted from AumBot-Web and adapted for AUM NEXUS POLY.
LLM calls replaced with llm_call() from llm.py.
"""
import json
import math
import re
import requests
from datetime import datetime, timezone
from llm import llm_call

EVENTS_URL = "https://gamma-api.polymarket.com/events"
MARKETS_URL = "https://gamma-api.polymarket.com/markets"

MARKET_NOT_FOUND_MSG = (
    "❌ Marché non trouvé dans le top Polymarket.\n"
    "💡 Essaie un sujet plus précis en anglais."
)


def _parse_prices(market: dict) -> list:
    raw = market.get("outcomePrices", '["0.5","0.5"]')
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return ["0.5", "0.5"]
    return raw if isinstance(raw, list) else ["0.5", "0.5"]


def kelly_fraction(yes_float: float, edge_pts: float) -> float:
    """Quarter-Kelly, cap 5% bankroll."""
    true_p = yes_float + (edge_pts / 100)
    true_p = max(0.01, min(0.99, true_p))
    if yes_float <= 0:
        return 0.0
    market_odds = (1 - yes_float) / yes_float
    full_kelly = (true_p * market_odds - (1 - true_p)) / market_odds
    return round(max(0.0, min(full_kelly * 0.25, 0.05)), 4)


def calculate_net_verdict(yes_float: float, true_prob_float: float) -> dict:
    """Python PUR — NET, verdict, certitude, Kelly. Never LLM."""
    net_pts = round((true_prob_float - yes_float) * 100, 1)

    if net_pts > 15:
        verdict, certitude, reco = "SOUS-ESTIMÉ", "L3", "PAPER YES"
    elif net_pts > 5:
        verdict, certitude, reco = "SOUS-ESTIMÉ", "L2", "PAPER YES"
    elif net_pts >= -5:
        verdict, certitude, reco = "ALIGNÉ", "L1", "SKIP"
    elif net_pts >= -15:
        verdict, certitude, reco = "SURESTIMÉ", "L2", "PAPER NO"
    else:
        verdict, certitude, reco = "SURESTIMÉ", "L3", "PAPER NO"

    k = kelly_fraction(yes_float, net_pts)

    return {
        "net_pts": net_pts,
        "verdict": verdict,
        "certitude": certitude,
        "recommandation": reco,
        "kelly_pct": k,
    }


def maicr_score(market: dict) -> dict:
    """Score MAICR /100 — Python PUR, zéro LLM."""
    score = 0

    vol = float(market.get("volume", 0) or 0)
    vol_s = min(30, int(math.log10(max(vol, 1)) * 6))
    score += vol_s

    liq = float(market.get("liquidity", 0) or 0)
    liq_s = min(25, int(math.log10(max(liq, 1)) * 5))
    score += liq_s

    try:
        prices = _parse_prices(market)
        p = float(prices[0]) if prices else 0.5
        tension_s = int((1 - abs(p - 0.5) * 2) * 25)
        yes_pct = f"{p:.0%}"
    except Exception:
        tension_s = 0
        p = 0.5
        yes_pct = "50%"
    score += tension_s

    try:
        end = market.get("endDate", "")
        if end:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            jours = (end_dt - datetime.now(timezone.utc)).days
            if 7 <= jours <= 60:
                urg_s = 20
            elif jours < 7:
                urg_s = 5
            elif jours <= 180:
                urg_s = 12
            else:
                urg_s = 3
        else:
            jours = -1
            urg_s = 0
    except Exception:
        jours = -1
        urg_s = 0
    score += urg_s

    if p < 0.20:
        longshot_flag = True
        edge_bias = "LONGSHOT"
    elif p > 0.80:
        longshot_flag = False
        edge_bias = "FAVORI"
    else:
        longshot_flag = False
        edge_bias = "NEUTRE"

    return {
        "score":         min(score, 100),
        "vol_s":         vol_s,
        "liq_s":         liq_s,
        "tension_s":     tension_s,
        "urg_s":         urg_s,
        "question":      market.get("question", "?")[:80],
        "yes":           yes_pct,
        "yes_float":     p,
        "jours":         jours,
        "volume":        f"${vol / 1000:.0f}k",
        "liquidity":     f"${liq / 1000:.0f}k",
        "slug":          market.get("slug", ""),
        "url":           f"https://polymarket.com/event/{market.get('slug', '')}",
        "longshot_flag": longshot_flag,
        "edge_bias":     edge_bias,
        "event_id":      market.get("conditionId", market.get("id", "")),
    }


def fetch_markets(limit: int = 100) -> list:
    """Fetch top markets by volume. YES 5-95% only."""
    try:
        r = requests.get(
            EVENTS_URL,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
                "limit": limit,
            },
            timeout=15,
        )
        if not r.ok:
            return []
        events = r.json()
    except Exception:
        return []

    markets: list = []
    for event in events:
        for m in event.get("markets", []):
            prices = _parse_prices(m)
            try:
                yes = float(prices[0])
            except Exception:
                continue
            if not (0.05 <= yes <= 0.95):
                continue
            m["outcomePrices"] = prices
            m["_yes"] = yes
            markets.append(m)

    markets.sort(key=lambda x: float(x.get("volume", 0) or 0), reverse=True)
    return markets[:15]


def fetch_markets_day() -> list:
    """Fetch markets closing in 0-24h."""
    try:
        r = requests.get(
            EVENTS_URL,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
                "limit": 200,
            },
            timeout=15,
        )
        if not r.ok:
            return []
        events = r.json()
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    markets: list = []
    for event in events:
        for m in event.get("markets", []):
            try:
                end = m.get("endDate", "")
                if not end:
                    continue
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                heures = (end_dt - now).total_seconds() / 3600
                if not (0 < heures <= 24):
                    continue
                m["_hours"] = f"{heures:.1f}h"
            except Exception:
                continue
            prices = _parse_prices(m)
            try:
                yes = float(prices[0])
            except Exception:
                continue
            if not (0.05 <= yes <= 0.95):
                continue
            m["outcomePrices"] = prices
            m["_yes"] = yes
            markets.append(m)

    markets.sort(key=lambda x: float(x.get("volume", 0) or 0), reverse=True)
    return markets[:15]


def enrich_with_hours(scored: list, markets: list) -> list:
    now = datetime.now(timezone.utc)
    for m in scored:
        try:
            raw = next(
                mk.get("endDate", "")
                for mk in markets
                if mk.get("question", "")[:80] == m["question"]
            )
            end_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            heures = (end_dt - now).total_seconds() / 3600
            m["heures"] = f"{heures:.1f}h"
        except Exception:
            m["heures"] = "~24h"
    return scored


def fetch_live_polymarket_odds(topic: str) -> dict | None:
    """
    Find the best-matching Polymarket market for a topic using LLM selection.
    Returns market dict or None.
    """
    try:
        r = requests.get(
            EVENTS_URL,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
                "limit": 200,
                "liquidity_num_min": 1000,
            },
            timeout=15,
        )
        if not r.ok:
            return None
        events = r.json()
    except Exception:
        return None

    markets: list = []
    for event in events:
        for m in event.get("markets", []):
            prices_raw = m.get("outcomePrices", '["0.5","0.5"]')
            if isinstance(prices_raw, str):
                try:
                    m["outcomePrices"] = json.loads(prices_raw)
                except Exception:
                    m["outcomePrices"] = ["0.5", "0.5"]
            try:
                yes_p = float(m["outcomePrices"][0])
            except Exception:
                yes_p = 0.5
            if yes_p == 0.0 or yes_p == 1.0:
                continue
            liq = float(m.get("liquidity", 0) or 0)
            if liq < 1000:
                continue
            markets.append(m)

    if not markets:
        return None

    markets.sort(key=lambda x: float(x.get("volume", 0) or 0), reverse=True)

    # Expand topic keywords
    fallback_terms = list({w.lower().strip("'\".,!?") for w in topic.split() if len(w) >= 3})
    expand_prompt = (
        f"Topic: '{topic}'\n"
        f"List 4-8 English search terms to find prediction markets about this topic.\n"
        f'JSON only: ["term1","term2",...]'
    )
    try:
        raw = llm_call(expand_prompt, max_tokens=150)
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start != -1 and end > 0:
            search_terms = [t.lower().strip() for t in json.loads(raw[start:end]) if isinstance(t, str)]
        else:
            search_terms = fallback_terms
    except Exception:
        search_terms = fallback_terms

    def _match(q: str) -> bool:
        q = q.lower()
        return any(t in q for t in search_terms)

    filtered = [m for m in markets[:200] if _match(m.get("question", ""))]
    candidates = filtered[:20] if filtered else markets[:20]

    today = datetime.now(timezone.utc)
    today_str = today.strftime("%B %d, %Y")

    def _days_left(m: dict) -> str:
        try:
            end = m.get("endDate", "")
            if not end:
                return "?"
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            days = (end_dt - today).days
            return f"{days}d" if days >= 0 else "expired"
        except Exception:
            return "?"

    lines = [
        f"[{i}] {m.get('question', '?')[:80]} | ${float(m.get('volume', 0) or 0)/1000:.0f}k | expires {_days_left(m)}"
        for i, m in enumerate(candidates)
    ]
    select_prompt = (
        f"Today is {today_str}. Topic: '{topic}'\n\n"
        f"Polymarket markets:\n" + "\n".join(lines) + "\n\n"
        f"Which index matches best? Return -1 if no genuine match.\n"
        f'JSON only: {{"index": <N or -1>, "confidence": <0.0-1.0>}}'
    )
    try:
        raw = llm_call(select_prompt, max_tokens=100)
        start = raw.find("{")
        end = raw.rfind("}") + 1
        result = json.loads(raw[start:end])
        idx = int(result.get("index", -1))
        confidence = float(result.get("confidence", 0.0))
    except Exception:
        return None

    if idx == -1 or confidence < 0.6 or not (0 <= idx < len(candidates)):
        return None

    m = candidates[idx]
    prices = m.get("outcomePrices", ["0.5", "0.5"])
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = ["0.5", "0.5"]

    try:
        yes_guard = float(prices[0])
    except Exception:
        yes_guard = 0.5
    if yes_guard < 0.05 or yes_guard > 0.95:
        return None

    vol = float(m.get("volume", 0) or 0)
    liq = float(m.get("liquidity", 0) or 0)
    return {
        "question": m.get("question", "?")[:100],
        "yes_odds": int(yes_guard * 100),
        "no_odds":  int((1 - yes_guard) * 100),
        "yes_float": yes_guard,
        "volume":   f"${vol/1000:.0f}k",
        "liquidity": f"${liq/1000:.0f}k",
        "end_date": (m.get("endDate") or "?")[:10],
        "slug":     m.get("slug", ""),
        "url":      f"https://polymarket.com/event/{m.get('slug', '')}",
    }


def build_bull_bear_context(top3: list, osint_snippets: dict = None) -> str:
    """Prompt for LLM Bull vs Bear analysis. NET calculated in Python, not LLM."""
    osint_snippets = osint_snippets or {}
    ctx = "DONNÉES MAICR PRÉ-CALCULÉES — ANALYSE BULL vs BEAR\n\n"

    for i, m in enumerate(top3, 1):
        bias_note = ""
        if m.get("edge_bias") == "LONGSHOT":
            bias_note = (
                "\n⚠️ LONGSHOT BIAS: YES < 20%. "
                "Marchés à faible probabilité systématiquement surévalués par les traders retail."
            )
        elif m.get("edge_bias") == "FAVORI":
            bias_note = (
                "\n⚠️ FAVORI BIAS: YES > 80%. "
                "Les favoris sont souvent sous-évalués."
            )

        osint_note = ""
        q = m.get("question", "")
        if q in osint_snippets and osint_snippets[q]:
            osint_note = f"\nDONNÉES RÉELLES: {osint_snippets[q]}"

        ctx += (
            f"MARCHÉ #{i} — Score {m['score']}/100\n"
            f"Question: {m['question']}\n"
            f"YES: {m['yes']} | Volume: {m['volume']} | J-{m['jours']}\n"
            f"URL: {m['url']}{bias_note}{osint_note}\n\n"
        )

    ctx += (
        "INSTRUCTIONS STRICTES:\n\n"
        "Pour chaque marché:\n"
        "1. Donne 1 argument BULL (pour YES) et 1 argument BEAR (pour NO). Dense, factuel.\n"
        "2. Estime ta PROBABILITÉ VRAIE en % (ex: 67%). "
        "Base-toi sur les données + biais identifiés.\n"
        "3. NE calcule PAS le NET. Donne uniquement ta probabilité vraie — Python calculera le verdict.\n\n"
        "Format EXACT pour chaque marché:\n\n"
        "🎯 MAICR — #{numéro} {question}\n"
        "YES: X% | Vol: $Xk | J-{jours}\n"
        "🔗 {url}\n\n"
        "🟢 BULL: [1 phrase dense et factuelle]\n\n"
        "🔴 BEAR: [1 phrase dense et factuelle]\n\n"
        "📐 PROBA VRAIE ESTIMÉE: X%\n\n"
        "---\n\n"
        "Répète ce format pour les 3 marchés. Sans blabla. Sans inventer."
    )
    return ctx


def build_future_llm_context(live_market: dict) -> str:
    """Prompt for Crystal Ball analysis of a specific market."""
    return (
        f"Analyse ce marché Polymarket en profondeur.\n\n"
        f"Question: {live_market['question']}\n"
        f"YES: {live_market['yes_odds']}% | NO: {live_market['no_odds']}%\n"
        f"Volume: {live_market['volume']} | Liquidité: {live_market['liquidity']}\n"
        f"Expiration: {live_market['end_date']}\n\n"
        f"1. Donne 2 arguments BULL solides\n"
        f"2. Donne 2 arguments BEAR solides\n"
        f"3. Estime la PROBABILITÉ VRAIE (%) et explique ton raisonnement\n"
        f"4. Identifie le biais de marché principal\n\n"
        f"Sois factuel, dense, 8-10 lignes max."
    )


def run_future_osint(topic: str) -> dict:
    """Stub — web search not available in standalone mode."""
    return {topic: {"text": "OSINT web search not available in standalone mode.", "urls": []}}


def format_scores_message(top3: list) -> str:
    lines = ["🎯 *MAICR Polymarket — Scores pré-LLM*\n"]
    for i, m in enumerate(top3, 1):
        bias_tag = f" ⚠️ {m.get('edge_bias', '')}" if m.get("longshot_flag") else ""
        lines.append(
            f"*#{i} — {m['score']}/100*{bias_tag}\n"
            f"📊 {m['question']}\n"
            f"YES: {m['yes']} | {m['volume']} | J-{m['jours']}\n"
            f"_{m['vol_s']}/30  {m['liq_s']}/25  "
            f"{m['tension_s']}/25  {m['urg_s']}/20_\n"
            f"🔗 {m['url']}\n"
        )
    return "\n".join(lines)


def format_day_scores_message(top3: list) -> str:
    lines = ["🎯 *MAICR Polymarket — Marchés J-24h*\n"]
    for i, m in enumerate(top3, 1):
        bias_tag = f" ⚠️ {m.get('edge_bias', '')}" if m.get("longshot_flag") else ""
        lines.append(
            f"*#{i} — {m['score']}/100*{bias_tag}\n"
            f"📊 {m['question']}\n"
            f"YES: {m['yes']} | {m['volume']} | ⏰ {m.get('heures', '?')} restantes\n"
            f"_{m['vol_s']}/30  {m['liq_s']}/25  "
            f"{m['tension_s']}/25  {m['urg_s']}/20_\n"
            f"🔗 {m['url']}\n"
        )
    return "\n".join(lines)


def format_bull_bear_with_verdict(llm_output: str, top3: list) -> str:
    """
    Post-process LLM Bull/Bear output.
    Extract true probabilities and inject NET/verdict/Kelly calculated in Python.
    """
    lines_out = []
    sections = llm_output.split("---")

    for idx, section in enumerate(sections):
        if not section.strip():
            continue

        match = re.search(r"PROBA VRAIE ESTIMÉE\s*:\s*(\d+(?:\.\d+)?)\s*%", section)

        if match and idx < len(top3):
            true_prob = float(match.group(1)) / 100
            yes_float = top3[idx].get("yes_float", 0.5)
            result = calculate_net_verdict(yes_float, true_prob)

            net_line = (
                f"\n⚖️ NET: {result['net_pts']:+.1f}pts — "
                f"{result['verdict']} ({result['certitude']})\n"
                f"👉 {result['recommandation']}\n"
                f"💰 KELLY: {result['kelly_pct']*100:.1f}% bankroll\n"
            )
            section = section + net_line

        lines_out.append(section.strip())

    return "\n\n---\n\n".join(lines_out)


def check_intramarket_coherence(markets_same_event: list) -> dict:
    if not markets_same_event:
        return {"arbitrage_detected": False, "sum_yes": 0.0, "edge_cents": 0.0}
    sum_yes = sum(m.get("yes_float", 0.5) for m in markets_same_event)
    edge_cents = round((sum_yes - 1.0) * 100, 2)
    return {
        "arbitrage_detected": sum_yes > 1.02,
        "sum_yes": round(sum_yes, 4),
        "edge_cents": edge_cents,
    }
