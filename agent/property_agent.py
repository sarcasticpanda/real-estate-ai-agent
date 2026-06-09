"""
Main agent loop. Handles one user message and returns Riya's reply.

Lead capture flow:
  "none"   — just search and recommend
  "soft"   — show properties + gently nudge ("want me to set up a visit?")
  "strong" — move to lead_capture stage → collect name + phone
  Auto-nudge after 2+ recommendation turns with no action
"""

import os
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from groq import Groq
from agent.conversation_manager import ConversationManager
from agent.intent_extractor import extract_intent, merge_requirements
from agent.lead_collector import extract_name_and_phone, create_lead, notify_broker_via_n8n
from rag.retriever import retrieve, format_properties_for_llm
from rag.prompts import (
    SYSTEM_PROMPT, PROPERTY_RECOMMENDATION_PROMPT, LEAD_CAPTURE_PROMPT,
    NO_RESULTS_PROMPT, SOFT_INTEREST_PROMPT, CLARIFY_PROMPT,
    LEAD_SAVED_TEMPLATE,
)

logger = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.1-8b-instant"
# Automatically nudge toward lead capture after this many recommendation turns
_AUTO_NUDGE_AFTER = 2


def _groq_client() -> Groq:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set in .env")
    return Groq(api_key=key)


def _llm(messages: list[dict], temperature: float = 0.7, max_tokens: int = 700) -> str:
    client = _groq_client()
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()


def process_message(session_id: str, user_message: str, platform: str = "web") -> str:
    """
    Process one user message and return Riya's reply.
    Single entry point for Telegram bot, web chat, API.
    """
    conv = ConversationManager(session_id, platform)
    conv.load()
    conv.add_user_message(user_message)

    reply = _route(conv, user_message)

    conv.add_assistant_message(reply)
    conv.save()
    return reply


def _route(conv: ConversationManager, user_message: str) -> str:
    # ── Stage: collecting name + phone ───────────────────────────────────────
    if conv.is_lead_capture_stage():
        return _handle_lead_capture(conv, user_message)

    # ── Extract intent ────────────────────────────────────────────────────────
    extracted = extract_intent(user_message, conv.get_history_for_llm())
    conv.requirements = merge_requirements(conv.requirements, extracted)
    lead_level = extracted.get("lead_intent_level", "none")

    # ── Strong intent → move to lead capture ─────────────────────────────────
    if lead_level == "strong":
        conv.set_stage("lead_capture")
        return _ask_for_contact(conv)

    # ── Search + recommend ────────────────────────────────────────────────────
    if conv.has_enough_info():
        conv.set_stage("recommending")
        reply = _recommend(conv, user_message, lead_level)
    else:
        reply = _clarify(conv, user_message)

    return reply


def _recommend(conv: ConversationManager, user_message: str, lead_level: str) -> str:
    """Run retrieval and generate recommendation. Append soft nudge if appropriate."""
    properties = retrieve(user_message, conv.requirements, top_k=5)

    if properties:
        props_text = format_properties_for_llm(properties)
        user_prompt = PROPERTY_RECOMMENDATION_PROMPT.format(
            count=len(properties),
            requirements=_requirements_summary(conv.requirements),
            properties_text=props_text,
        )
    else:
        user_prompt = NO_RESULTS_PROMPT.format(
            requirements=_requirements_summary(conv.requirements)
        )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += conv.get_history_for_llm()
    messages.append({"role": "user", "content": user_prompt})

    reply = _llm(messages)

    # Soft nudge: user showed mild interest OR already recommended twice with no action
    rec_count = conv.get_recommendation_count()
    should_nudge = (lead_level == "soft") or (rec_count >= _AUTO_NUDGE_AFTER and lead_level == "none")

    if should_nudge and properties:
        conv.increment_recommendation_count()
        nudge_prompt = SOFT_INTEREST_PROMPT.format(
            context=_requirements_summary(conv.requirements),
            message=user_message,
        )
        nudge_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        nudge_messages.append({"role": "assistant", "content": reply})
        nudge_messages.append({"role": "user", "content": nudge_prompt})
        reply = _llm(nudge_messages, temperature=0.5, max_tokens=200)
    else:
        conv.increment_recommendation_count()

    return reply


def _handle_lead_capture(conv: ConversationManager, user_message: str) -> str:
    """Try to extract name + phone. Keep asking gently until we get both."""
    name, phone = extract_name_and_phone(user_message)

    if name and phone:
        lead = create_lead(
            session_id=conv.session_id,
            requirements=conv.requirements,
            name=name,
            phone=phone,
        )
        if lead:
            notify_broker_via_n8n(lead, conv.requirements)
            conv.set_stage("done")
            return LEAD_SAVED_TEMPLATE.format(name=name, phone=phone)
        else:
            return "Oops, something went wrong on my end! Can you share your name and number once more?"

    if phone:
        return "Got your number! Could you also share your name? The broker would love to know who they're calling 😊"

    # Nothing extracted — ask again warmly
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += conv.get_history_for_llm()
    messages.append({
        "role": "user",
        "content": (
            "The user wants to proceed but hasn't shared their name and phone yet. "
            "Gently ask again in Riya's warm style — one casual sentence. "
            "Remind them the broker will call them (not text) to schedule a visit."
        ),
    })
    return _llm(messages, temperature=0.5, max_tokens=120)


def _ask_for_contact(conv: ConversationManager) -> str:
    """Generate the initial lead capture ask."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += conv.get_history_for_llm()
    messages.append({
        "role": "user",
        "content": LEAD_CAPTURE_PROMPT.format(
            context=_requirements_summary(conv.requirements)
        ),
    })
    return _llm(messages, temperature=0.5, max_tokens=150)


def _clarify(conv: ConversationManager, user_message: str) -> str:
    """Ask for the most important missing requirement."""
    known_parts = []
    r = conv.requirements
    if r.get("bhk"):
        known_parts.append(f"{r['bhk']} BHK")
    if r.get("area"):
        known_parts.append(r["area"])
    if r.get("max_budget_cr"):
        known_parts.append(f"under {r['max_budget_cr']} crore")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += conv.get_history_for_llm()
    messages.append({
        "role": "user",
        "content": CLARIFY_PROMPT.format(
            message=user_message,
            known=", ".join(known_parts) if known_parts else "nothing yet",
        ),
    })
    return _llm(messages, temperature=0.6, max_tokens=150)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _requirements_summary(req: dict) -> str:
    parts = []
    if req.get("bhk"):
        parts.append(f"{req['bhk']} BHK")
    if req.get("property_type"):
        parts.append(req["property_type"])
    if req.get("area"):
        parts.append(f"in {req['area']}")
    if req.get("city"):
        parts.append(f"({req['city']})")
    if req.get("max_budget_cr"):
        parts.append(f"under {req['max_budget_cr']} crore")
    if req.get("min_budget_cr"):
        parts.append(f"above {req['min_budget_cr']} crore")
    if req.get("amenities"):
        parts.append(f"with {', '.join(req['amenities'])}")
    if req.get("nearby"):
        parts.append(f"near {', '.join(req['nearby'])}")
    if req.get("named_landmark"):
        max_km = req.get("named_landmark_max_km", 5)
        parts.append(f"within {max_km} km of {req['named_landmark']}")
    return " ".join(parts) if parts else "open search"
