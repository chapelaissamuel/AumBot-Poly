"""
LLM stack — priority chain: Gemini 2.5 Flash → Groq Llama 3.3 70B → OpenRouter fallback.
Single public function: llm_call(prompt, max_tokens=500) → str
Never crashes. Always returns a string.
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

TIMEOUT = 20


def _gemini(prompt: str, max_tokens: int) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("No GEMINI_API_KEY")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    resp = requests.post(url, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _groq(prompt: str, max_tokens: int) -> str:
    if not GROQ_API_KEY:
        raise ValueError("No GROQ_API_KEY")
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _openrouter(prompt: str, max_tokens: int) -> str:
    if not OPENROUTER_API_KEY:
        raise ValueError("No OPENROUTER_API_KEY")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://aumnexuspoly.replit.app",
            "X-Title": "AUM NEXUS POLY",
        },
        json={
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def llm_call(prompt: str, max_tokens: int = 500) -> str:
    """
    Try Gemini → Groq → OpenRouter in order.
    Returns the first successful response, or an error string as last resort.
    """
    providers = [
        ("Gemini 2.5 Flash", _gemini),
        ("Groq Llama-3.3 70B", _groq),
        ("OpenRouter Llama-3.3 70B", _openrouter),
    ]
    last_error = "All LLM providers failed."
    for name, fn in providers:
        try:
            result = fn(prompt, max_tokens)
            if result and result.strip():
                logger.info("[LLM] success via %s", name)
                return result.strip()
        except Exception as exc:
            logger.warning("[LLM] %s failed: %s", name, exc)
            last_error = str(exc)
    logger.error("[LLM] all providers failed: %s", last_error)
    return f"[LLM indisponible — {last_error}]"
