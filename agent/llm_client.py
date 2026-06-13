"""
Unified LLM client with automatic fallback.

Primary:  Google Gemini Flash (free tier: ~1,500 req/day, 1M tokens/min) — smartest,
          and its huge token-per-minute limit avoids the rate-limit (429) problems
          we hit with Groq's small free quota.
Fallback: Groq Llama-3.3-70B (free, fast) — used when GEMINI_API_KEY is unset, or when
          Gemini errors / is rate-limited.

Both call sites (intent extraction + chat generation) go through complete().
Gemini is called over plain REST (requests) so there is no extra SDK dependency to
break — the API key is the only thing needed.

Env:
  GEMINI_API_KEY   free key from https://aistudio.google.com/apikey (optional)
  GEMINI_MODEL     default "gemini-2.5-flash"
  GROQ_API_KEY     existing
  GROQ_MODEL       default "llama-3.3-70b-versatile"
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

_groq_client = None


def complete(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 700,
    json_mode: bool = False,
) -> str:
    """
    Chat completion with Gemini-primary, Groq-fallback.

    messages: OpenAI-style [{"role": "system"|"user"|"assistant", "content": str}, ...]
    json_mode: ask the model to return a single JSON object.
    Returns the assistant text (JSON string if json_mode).
    """
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        try:
            return _gemini_complete(messages, temperature, max_tokens, json_mode, gemini_key)
        except Exception as e:
            logger.warning(f"[llm] Gemini failed ({type(e).__name__}: {str(e)[:120]}) — falling back to Groq")
    return _groq_complete(messages, temperature, max_tokens, json_mode)


# ── Gemini (REST) ─────────────────────────────────────────────────────────────

def _gemini_complete(messages, temperature, max_tokens, json_mode, key) -> str:
    # Gemini separates the system instruction from the turn contents.
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    contents = []
    for m in messages:
        if m["role"] == "system":
            continue
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    gen_cfg = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        # Disable "thinking" — for intent extraction + short chat replies it only burns
        # the output-token budget (and can yield an empty response). Ignored by models
        # that don't support it.
        "thinkingConfig": {"thinkingBudget": 0},
    }
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"

    body = {"contents": contents, "generationConfig": gen_cfg}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={key}"
    )
    resp = requests.post(url, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {str(data)[:160]}")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Gemini returned empty text")
    return text


# ── Groq (SDK) ────────────────────────────────────────────────────────────────

def _get_groq():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY not set in .env")
        _groq_client = Groq(api_key=key)
    return _groq_client


def _groq_complete(messages, temperature, max_tokens, json_mode) -> str:
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _get_groq().chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs,
    )
    return resp.choices[0].message.content.strip()
