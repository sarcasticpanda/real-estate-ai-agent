"""
Uses Groq (LLaMA 3.1 8B Instant — free tier) to extract structured
buyer requirements from a natural language message.
"""

import os
import re
import json
import logging
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_client = None


def _get_groq_client() -> Groq:
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set in .env — get a free key at console.groq.com")
        _client = Groq(api_key=api_key)
    return _client


def extract_intent(message: str, conversation_history: list[dict] | None = None) -> dict:
    """
    Extract structured requirements from the user's message.
    Returns a dict with city, area, bhk, budget, amenities, nearby, lead_intent_level, etc.
    """
    from rag.prompts import INTENT_EXTRACTION_PROMPT

    history_text = ""
    if conversation_history:
        recent = conversation_history[-6:]
        history_text = "\n".join(
            f"{m['role'].title()}: {m['content']}" for m in recent
        )

    prompt = INTENT_EXTRACTION_PROMPT.format(message=message, history=history_text)

    try:
        client = _get_groq_client()
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,  # deterministic for structured extraction
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        extracted = json.loads(raw)
        logger.info(f"Extracted intent: {extracted}")
        result = _normalise(extracted)
        result = _postprocess(result, message)
        return result

    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Intent extraction failed: {e} — returning empty requirements")
        return _empty_requirements()


def merge_requirements(existing: dict, new_extraction: dict) -> dict:
    """
    Accumulate requirements across conversation turns.
    New non-null values override existing. Lists are merged (deduplicated).
    """
    merged = dict(existing)
    for key, value in new_extraction.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            existing_list = merged.get(key, []) or []
            combined = list(dict.fromkeys(existing_list + value))  # dedup, preserve order
            merged[key] = combined
        else:
            merged[key] = value
    return merged


# ── Internal helpers ──────────────────────────────────────────────────────────

_UPPER_LIMIT_KEYWORDS = re.compile(
    r"\b(under|below|within|up to|upto|not more than|max|maximum|budget hai|budget|mere paas|cost|worth)\b",
    re.IGNORECASE,
)
_LOWER_LIMIT_KEYWORDS = re.compile(
    r"\b(above|at least|minimum|min|starting from|more than|atleast)\b",
    re.IGNORECASE,
)
_IN_AREA_RE = re.compile(
    r"\b(?:in|at|near|around)\s+([A-Z][a-zA-Z ]{2,25}?)(?:\s+(?:under|above|below|near|with|for|\d)|$)",
    re.IGNORECASE,
)

# Known Lucknow area names (lowercase for comparison)
_LUCKNOW_AREAS = {
    "gomti nagar", "gomtinagar", "gomti nagar extension", "gomtinagar extension",
    "aliganj", "indira nagar", "indiranagar", "hazratganj", "ashiana",
    "alambagh", "chowk", "aminabad", "mahanagar", "raj bhavan road",
    "thakurganj", "kapoorthala", "vikas nagar", "jankipuram",
    "kursi road", "faizabad road", "sultanpur road", "rae bareli road",
    "chinhat", "sarojini nagar", "transport nagar", "vrindavan yojna",
    "sushant golf city", "kalyanpur",
}


def _postprocess(result: dict, original_message: str) -> dict:
    """Python-side guardrails for common LLM extraction mistakes."""
    msg_lower = original_message.lower()

    # ── Fix city/area confusion ──────────────────────────────────────────────
    # LLM sometimes puts a neighbourhood (e.g. "Gomti Nagar") in city instead of area
    city = result.get("city", "")
    if city and city.lower() in _LUCKNOW_AREAS and city.lower() != "lucknow":
        if not result.get("area"):
            result["area"] = city.title()
        result["city"] = "Lucknow"
        logger.info(f"[postprocess] city '{city}' is a Lucknow area — moved to area, city=Lucknow")

    # ── Fix budget direction ─────────────────────────────────────────────────
    # If LLM put budget in min but the message says "under/below/within/budget X"
    min_cr = result.get("min_budget_cr")
    max_cr = result.get("max_budget_cr")

    if min_cr is not None and max_cr is None:
        if _UPPER_LIMIT_KEYWORDS.search(msg_lower):
            # Swap: this is actually an upper limit
            result["max_budget_cr"] = min_cr
            result["min_budget_cr"] = None
            logger.info(f"[postprocess] Budget direction corrected: min→max ({min_cr})")

    if max_cr is not None and min_cr is None:
        if _LOWER_LIMIT_KEYWORDS.search(msg_lower) and not _UPPER_LIMIT_KEYWORDS.search(msg_lower):
            # Swap: this is actually a lower limit
            result["min_budget_cr"] = max_cr
            result["max_budget_cr"] = None
            logger.info(f"[postprocess] Budget direction corrected: max→min ({max_cr})")

    # ── Fix missing area ─────────────────────────────────────────────────────
    if not result.get("area"):
        # Try regex "in/at/near [Area Name]"
        m = _IN_AREA_RE.search(original_message)
        if m:
            candidate = m.group(1).strip().lower()
            # Check if it resembles a known Lucknow area (fuzzy: any known area keyword)
            for known in _LUCKNOW_AREAS:
                if candidate in known or known in candidate or known.split()[0] in candidate:
                    result["area"] = m.group(1).strip().title()
                    logger.info(f"[postprocess] Area rescued: '{result['area']}'")
                    break

    return result


def _normalise(extracted: dict) -> dict:
    """Sanitise and fill defaults on raw Groq output."""
    result = _empty_requirements()

    for key in ["city", "area", "bhk", "min_budget_cr", "max_budget_cr",
                "property_type", "furnishing", "intent",
                "named_landmark", "named_landmark_max_km"]:
        v = extracted.get(key)
        if v is not None and v != "null":
            result[key] = v

    for list_key in ["amenities", "nearby"]:
        v = extracted.get(list_key)
        if isinstance(v, list) and v:
            result[list_key] = [str(x) for x in v]

    # lead_intent_level: "none" | "soft" | "strong"
    level = str(extracted.get("lead_intent_level", "none")).lower()
    if level not in ("none", "soft", "strong"):
        level = "none"
    result["lead_intent_level"] = level

    # For backwards compatibility: is_lead_ready = strong intent
    result["is_lead_ready"] = (level == "strong")

    # Default city
    if not result.get("city"):
        result["city"] = "Lucknow"

    # Coerce types
    if result.get("bhk") is not None:
        result["bhk"] = int(result["bhk"])
    if result.get("max_budget_cr") is not None:
        result["max_budget_cr"] = float(result["max_budget_cr"])
    if result.get("min_budget_cr") is not None:
        result["min_budget_cr"] = float(result["min_budget_cr"])

    return result


def _empty_requirements() -> dict:
    return {
        "city": "Lucknow",
        "area": None,
        "bhk": None,
        "max_budget_cr": None,
        "min_budget_cr": None,
        "property_type": None,
        "furnishing": None,
        "amenities": [],
        "nearby": [],
        "named_landmark": None,
        "named_landmark_max_km": None,
        "intent": None,
        "lead_intent_level": "none",
        "is_lead_ready": False,
    }
