"""
Uses Groq (LLaMA 3.1 8B Instant — free tier) to extract structured
buyer requirements from a natural language message.
"""

import os
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

    Returns a dict like:
    {
        "city": "Lucknow",
        "area": "Gomti Nagar",
        "bhk": 3,
        "max_budget_cr": 2.0,
        "min_budget_cr": null,
        "property_type": "flat",
        "furnishing": null,
        "amenities": ["gym", "swimming pool"],
        "nearby": ["metro", "school"],
        "intent": "buy",
        "is_lead_ready": false
    }
    """
    from rag.prompts import INTENT_EXTRACTION_PROMPT

    prompt = INTENT_EXTRACTION_PROMPT.format(message=message)

    # Include recent conversation context for multi-turn understanding
    messages = []
    if conversation_history:
        # Last 4 exchanges for context
        messages = conversation_history[-8:]
    messages.append({"role": "user", "content": prompt})

    try:
        client = _get_groq_client()
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.1,  # low temperature for structured extraction
            max_tokens=400,
        )
        raw = response.choices[0].message.content.strip()

        # Clean up any markdown fences the model might add
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        extracted = json.loads(raw)
        logger.info(f"Extracted intent: {extracted}")
        return extracted

    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Intent extraction failed: {e} — returning empty requirements")
        return _empty_requirements()


def merge_requirements(existing: dict, new_extraction: dict) -> dict:
    """
    Merge newly extracted requirements into the session's accumulated requirements.
    New values override existing only when they are not null.
    """
    merged = dict(existing)
    for key, value in new_extraction.items():
        if value is not None and value != [] and value != "":
            merged[key] = value
    return merged


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
        "intent": None,
        "is_lead_ready": False,
    }
