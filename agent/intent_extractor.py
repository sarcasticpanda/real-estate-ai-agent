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
        result = _empty_requirements()
        result["_extraction_failed"] = True
        return result


def merge_requirements(existing: dict, new_extraction: dict, clear_area: bool = False) -> dict:
    """
    Accumulate requirements across conversation turns.
    New non-null values override existing. Lists are merged (deduplicated).

    clear_area=True: wipe the stored area so the next search covers all of Lucknow.
    """
    merged = dict(existing)
    if clear_area:
        merged["area"] = None

    for key, value in new_extraction.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            existing_list = merged.get(key, []) or []
            combined = list(dict.fromkeys(existing_list + value))
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
_LAKH_RE = re.compile(r"\b(lakh|lac|lakhs|lacs)\b", re.IGNORECASE)
_CRORE_RE = re.compile(r"\b(crore|cr|crores)\b", re.IGNORECASE)
_HAS_DIGIT_RE = re.compile(r"\d")
_BUDGET_WORD_RE = re.compile(r"\b(budget|price|cost|cheap|expensive|affordable|lakh|lac|crore|cr|rupees|rs)\b", re.IGNORECASE)
_IN_AREA_RE = re.compile(
    r"\b(?:in|at|near|around)\s+([A-Z][a-zA-Z ]{2,25}?)(?:\s+(?:under|above|below|near|with|for|\d)|$)",
    re.IGNORECASE,
)

# Capture the place phrase after a proximity word, for the landmark fallback.
_NEAR_PHRASE_RE = re.compile(
    r"\b(?:near|around|close to|next to|beside|nearby|close by to)\s+(?:the\s+)?"
    r"([a-zA-Z][a-zA-Z0-9 &.'\-]{2,45})",
    re.IGNORECASE,
)
# Generic place-type words — "near metro" is a connectivity request, not a landmark.
_GENERIC_PLACE_WORDS = {
    "metro", "station", "hospital", "school", "park", "market", "railway",
    "bus", "stop", "airport", "college", "temple", "highway", "road", "mall",
    "hospitals", "schools", "parks", "markets", "stations", "here", "me", "by",
    "it", "this", "that", "area", "areas", "location", "place", "places", "somewhere",
}
# Trailing filler to trim from a captured landmark phrase.
_LANDMARK_TRAILING_RE = re.compile(
    r"\b(instead|please|now|maybe|area|location|side|please show|under|below|above|with|for|priced).*$",
    re.IGNORECASE,
)

# Known Lucknow area names (lowercase for comparison)
_LUCKNOW_AREAS = {
    "gomti nagar", "gomtinagar", "gomti nagar extension", "gomtinagar extension",
    "aliganj", "indira nagar", "indiranagar", "hazratganj", "ashiana",
    "alambagh", "alam bagh", "chowk", "aminabad", "mahanagar", "raj bhavan road",
    "thakurganj", "kapoorthala", "vikas nagar", "jankipuram", "vibhuti khand",
    "kursi road", "faizabad road", "sultanpur road", "rae bareli road",
    "chinhat", "sarojini nagar", "transport nagar", "vrindavan yojna",
    "sushant golf city", "kalyanpur", "lucknow",
}

# Aliases → canonical name (handle typos, short forms)
_AREA_ALIASES: dict[str, str] = {
    "alam bagh": "Alambagh",
    "alambaag": "Alambagh",
    "aalam bagh": "Alambagh",
    "gomtinagar": "Gomti Nagar",
    "indiranagar": "Indira Nagar",
    "indra nagar": "Indira Nagar",
    "hazrat ganj": "Hazratganj",
    "sarojini ngr": "Sarojini Nagar",
    "vrindavan": "Vrindavan Yojna",
}


def _postprocess(result: dict, original_message: str) -> dict:
    """Python-side guardrails for common LLM extraction mistakes."""
    msg_lower = original_message.lower()

    # ── Guard: bhk must be a real bedroom count (1-10). 0/garbage → None ──────
    # Prevents nonsense like "0 BHK Shop" from a bad extraction sticking in session.
    bhk = result.get("bhk")
    if bhk is not None and (not isinstance(bhk, int) or bhk < 1 or bhk > 10):
        result["bhk"] = None
        logger.info(f"[postprocess] dropped invalid bhk={bhk}")

    # ── Guard: budget must be grounded in THIS message ───────────────────────
    # The LLM sees history and often re-emits a budget the user mentioned turns ago.
    # When merged that silently corrupts state (e.g. adds a stray min == stored max,
    # so "under 60 lakh" later becomes "between 60 and 75 lakh"). If the current
    # message has no number and no price word, drop the extracted budget so merge
    # keeps the real stored value instead.
    if not _HAS_DIGIT_RE.search(original_message) and not _BUDGET_WORD_RE.search(msg_lower):
        if result.get("min_budget_cr") is not None or result.get("max_budget_cr") is not None:
            logger.info(f"[postprocess] dropped ungrounded budget min={result.get('min_budget_cr')} "
                        f"max={result.get('max_budget_cr')} (no budget signal in message)")
            result["min_budget_cr"] = None
            result["max_budget_cr"] = None

    # ── Fix city/area confusion ──────────────────────────────────────────────
    # LLM sometimes puts a neighbourhood (e.g. "Gomti Nagar") in city instead of area
    city = result.get("city", "")
    if city and city.lower() in _LUCKNOW_AREAS and city.lower() != "lucknow":
        if not result.get("area"):
            result["area"] = city.title()
        result["city"] = "Lucknow"
        logger.info(f"[postprocess] city '{city}' is a Lucknow area — moved to area, city=Lucknow")

    # ── Fix lakh/crore unit confusion ────────────────────────────────────────
    # LLM often extracts "15 lakh" as 15.0 (treating it as crore).
    # If message mentions "lakh" but NOT "crore", divide any budget > 1 by 100.
    has_lakh  = bool(_LAKH_RE.search(msg_lower))
    has_crore = bool(_CRORE_RE.search(msg_lower))
    if has_lakh and not has_crore:
        for key in ("max_budget_cr", "min_budget_cr"):
            val = result.get(key)
            if val is not None and val >= 1.0:
                corrected = round(val / 100, 4)
                result[key] = corrected
                logger.info(f"[postprocess] Lakh conversion: {key} {val} → {corrected} crore")

    # ── Fix same min=max (LLM set both to same value for a single budget statement) ──
    # "budget 15 lakh" → min=0.15, max=0.15 → wrong; should be max=0.15, min=None
    min_cr = result.get("min_budget_cr")
    max_cr = result.get("max_budget_cr")
    if min_cr is not None and max_cr is not None and abs(min_cr - max_cr) < 0.001:
        if _LOWER_LIMIT_KEYWORDS.search(msg_lower) and not _UPPER_LIMIT_KEYWORDS.search(msg_lower):
            result["max_budget_cr"] = None  # explicit "above X" → min only
        else:
            result["min_budget_cr"] = None  # "budget X" → upper limit by default
            min_cr = None
        logger.info(f"[postprocess] Same min=max resolved → max={result.get('max_budget_cr')} min={result.get('min_budget_cr')}")

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

    # ── Fix missing area — scan entire message for known Lucknow areas ──────────
    # Catches "alambagh maybe", "like gomti nagar", "prefer aliganj" etc.
    if not result.get("area"):
        msg_lower_clean = re.sub(r"[,\.!?]", " ", msg_lower)
        words = msg_lower_clean.split()
        found_area = None
        # Try multi-word areas first (longest match), then single-word
        for known in sorted(_LUCKNOW_AREAS, key=len, reverse=True):
            if known == "lucknow":
                continue  # don't confuse city with area
            kw = known.split()
            for i in range(len(words) - len(kw) + 1):
                if words[i:i + len(kw)] == kw:
                    found_area = _AREA_ALIASES.get(known, known.title())
                    break
            if found_area:
                break
        if found_area:
            result["area"] = found_area
            logger.info(f"[postprocess] Area scanned from message: '{found_area}'")

    # ── Apply area aliases to LLM-extracted area ─────────────────────────────
    if result.get("area"):
        alias = _AREA_ALIASES.get(result["area"].lower())
        if alias:
            result["area"] = alias

    # ── Guard: named_landmark must not be a known area, and must appear in msg ──
    # The LLM sometimes (a) puts an area name into named_landmark, or (b) hallucinates
    # a landmark/nearby place the user never typed. Both break geocoding + retrieval.
    lm = result.get("named_landmark")
    if lm:
        lm_norm = lm.lower().strip()
        # (a) area mistaken for a landmark → move to area
        if lm_norm in _LUCKNOW_AREAS or lm_norm.replace(" ", "") in {a.replace(" ", "") for a in _LUCKNOW_AREAS}:
            if not result.get("area"):
                result["area"] = lm.title()
            result["named_landmark"] = None
            result["named_landmark_max_km"] = None
            logger.info(f"[postprocess] named_landmark '{lm}' is an area — moved to area")
        # (b) landmark not present in the original message → hallucination, drop it
        elif not _tokens_in_message(lm_norm, msg_lower):
            result["named_landmark"] = None
            result["named_landmark_max_km"] = None
            logger.info(f"[postprocess] named_landmark '{lm}' not in message — dropped (hallucination)")

    # ── Fallback: extract a named landmark the LLM missed ─────────────────────
    # "anything near phoenix united", "near Lulu Mall", "close to Ekana Stadium" — the
    # LLM is inconsistent about catching proper-noun places, so back it up in code.
    # Rule: take the phrase after near/around/close-to; if it is NOT a known area and is
    # NOT purely a generic place word (metro/hospital/...), treat it as a named landmark.
    if not result.get("named_landmark") and not result.get("area"):
        m = _NEAR_PHRASE_RE.search(original_message)
        if m:
            phrase = _clean_landmark_phrase(m.group(1))
            if phrase:
                p_norm = phrase.lower()
                is_area = p_norm in _LUCKNOW_AREAS or p_norm.replace(" ", "") in {a.replace(" ", "") for a in _LUCKNOW_AREAS}
                # strip generic place words; if anything proper-noun remains → landmark
                residual = " ".join(w for w in p_norm.split() if w not in _GENERIC_PLACE_WORDS).strip()
                if is_area:
                    result["area"] = phrase.title()
                    logger.info(f"[postprocess] near-phrase '{phrase}' is an area → area")
                elif residual:
                    result["named_landmark"] = phrase.title()
                    result["named_landmark_max_km"] = result.get("named_landmark_max_km") or 5.0
                    logger.info(f"[postprocess] landmark fallback extracted '{phrase}'")
                # else: purely generic ("near metro") — leave to nearby handling

    # ── Guard: drop hallucinated nearby entries not grounded in the message ────
    nearby = result.get("nearby") or []
    if nearby:
        kept = [n for n in nearby if _tokens_in_message(n.lower(), msg_lower)]
        if kept != nearby:
            logger.info(f"[postprocess] nearby filtered {nearby} → {kept} (removed ungrounded)")
        result["nearby"] = kept

    # ── Guard: "strong" intent REQUIRES an explicit action word in THIS message ──
    # The LLM occasionally over-classifies bare answers ("villa", "yes") as strong,
    # which wrongly jumps to lead capture. Enforce the invariant in code: no action
    # word → at most "soft". This makes routing deterministic regardless of the LLM.
    if result.get("lead_intent_level") == "strong" and not _ACTION_WORDS_RE.search(msg_lower):
        result["lead_intent_level"] = "soft"
        result["is_lead_ready"] = False
        logger.info("[postprocess] downgraded strong→soft (no action word in message)")

    return result


def _clean_landmark_phrase(raw: str) -> str:
    """Trim a captured 'near <X>' phrase down to the place name itself."""
    phrase = _LANDMARK_TRAILING_RE.sub("", raw).strip(" .,'-")
    # collapse whitespace; cap length to avoid swallowing a whole sentence
    phrase = re.sub(r"\s+", " ", phrase).strip()
    words = phrase.split()
    if len(words) > 5:
        words = words[:5]
    return " ".join(words)


# Action words that genuinely signal readiness to act (book/visit/contact NOW)
_ACTION_WORDS_RE = re.compile(
    r"\b(visit|book|schedule|arrange|appointment|site visit|"
    r"contact|call me|broker'?s? (number|contact)|give me .{0,15}number|"
    r"proceed|i'?ll take|take this|finalize|finalise|go ahead|"
    r"see (it|the .{1,12}) in person|meet)\b",
    re.IGNORECASE,
)


# Generic place words that ground a "nearby" entry even if the exact phrase differs
_NEARBY_ANCHORS = (
    "metro", "hospital", "school", "park", "market", "mall", "station",
    "railway", "bus", "airport", "college", "temple", "highway",
)


def _tokens_in_message(phrase: str, msg_lower: str) -> bool:
    """
    True if the extracted place is grounded in the user's message — either it appears
    as a substring, or it shares a meaningful place-anchor word (metro, mall, etc.)
    that is also present in the message. Prevents the LLM inventing places.
    """
    phrase = phrase.strip().lstrip("near ").strip()
    if not phrase:
        return False
    if phrase in msg_lower:
        return True
    # Any anchor word that is in BOTH the phrase and the message counts as grounded
    for anchor in _NEARBY_ANCHORS:
        if anchor in phrase and anchor in msg_lower:
            return True
    # Otherwise require at least one word of the phrase to appear in the message
    return any(w in msg_lower for w in phrase.split() if len(w) > 2)


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
