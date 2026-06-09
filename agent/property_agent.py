"""
Main agent loop. Handles one user message and returns the assistant's reply.

Flow:
  1. Load session
  2. Extract intent from message
  3. If in lead_capture stage → try to extract name/phone
  4. Otherwise → run RAG retrieval and generate response via Groq
  5. Save session
  6. Return reply text
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
from agent.intent_extractor import extract_intent
from agent.lead_collector import extract_name_and_phone, create_lead, notify_broker_via_n8n
from rag.retriever import retrieve, format_properties_for_llm
from rag.prompts import SYSTEM_PROMPT, PROPERTY_RECOMMENDATION_PROMPT, LEAD_CAPTURE_PROMPT, NO_RESULTS_PROMPT

logger = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.1-8b-instant"


def _groq_client() -> Groq:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set in .env")
    return Groq(api_key=key)


def _llm_chat(messages: list[dict], temperature: float = 0.7, max_tokens: int = 600) -> str:
    client = _groq_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()


def process_message(session_id: str, user_message: str, platform: str = "web") -> str:
    """
    Process one user message and return the assistant's reply.
    This is the single entry point called by Telegram bot, web chat, etc.
    """
    # Load conversation state
    conv = ConversationManager(session_id, platform)
    conv.load()
    conv.add_user_message(user_message)

    reply = ""

    # ── Stage: lead_capture ───────────────────────────────────────────────
    if conv.is_lead_capture_stage():
        name, phone = extract_name_and_phone(user_message)

        if name and phone:
            lead = create_lead(
                session_id=session_id,
                requirements=conv.requirements,
                name=name,
                phone=phone,
            )
            if lead:
                notify_broker_via_n8n(lead, conv.requirements)
                conv.set_stage("done")
                reply = (
                    f"Thank you, {name}! Your details have been shared with our broker. "
                    f"They will contact you at {phone} within 24 hours to schedule a property visit. "
                    f"Is there anything else I can help you with?"
                )
            else:
                reply = "I had trouble saving your details. Could you please share your name and phone number again?"
        elif phone:
            reply = "Got your number! Could you also share your name so the broker knows who to contact?"
        else:
            reply = (
                "Please share your full name and 10-digit mobile number "
                "so our broker can get in touch with you. For example: 'Raj Sharma, 9876543210'"
            )

        conv.add_assistant_message(reply)
        conv.save()
        return reply

    # ── Extract intent from message ───────────────────────────────────────
    extracted = extract_intent(user_message, conv.get_history_for_llm())
    conv.update_requirements(extracted)

    # Transition to lead capture if user is ready
    if extracted.get("is_lead_ready"):
        conv.set_stage("lead_capture")
        lead_prompt = LEAD_CAPTURE_PROMPT.format(context=str(conv.requirements))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conv.get_history_for_llm()
        messages.append({"role": "user", "content": lead_prompt})
        reply = _llm_chat(messages, temperature=0.5)
        conv.add_assistant_message(reply)
        conv.save()
        return reply

    # ── Retrieval + Recommendation ────────────────────────────────────────
    if conv.has_enough_info():
        conv.set_stage("recommending")
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

        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conv.get_history_for_llm()
        messages.append({"role": "user", "content": user_prompt})
        reply = _llm_chat(messages)
    else:
        # Not enough info yet — ask a clarifying question
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conv.get_history_for_llm()
        messages.append({
            "role": "user",
            "content": (
                "The user hasn't given enough details yet. "
                "Ask them one specific question to understand their requirements better. "
                "Keep it short and friendly. "
                "User message: " + user_message
            ),
        })
        reply = _llm_chat(messages, temperature=0.6, max_tokens=200)

    conv.add_assistant_message(reply)
    conv.save()
    return reply


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
        parts.append(f"under ₹{req['max_budget_cr']} Cr")
    if req.get("amenities"):
        parts.append(f"with {', '.join(req['amenities'])}")
    if req.get("nearby"):
        parts.append(f"near {', '.join(req['nearby'])}")
    return " ".join(parts) if parts else "unspecified requirements"
