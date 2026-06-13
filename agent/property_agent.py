"""
Main agent loop. Handles one user message and returns Riya's reply.

Lead capture flow:
  "none"   — just search and recommend
  "soft"   — show properties + track which property interested them
  "strong" — move to lead_capture stage -> collect name + phone
  Auto-nudge after 3+ recommendation turns with no action

Web onboarding:
  New web sessions collect name -> phone before property search, same as Telegram.
  Stored in session requirements._profile with onboarding_step tracking.
"""

import os
import re
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
    SYSTEM_PROMPT, SYSTEM_PROMPT_NAMED,
    PROPERTY_RECOMMENDATION_PROMPT, LEAD_CAPTURE_PROMPT,
    NO_RESULTS_PROMPT, SOFT_INTEREST_PROMPT, CLARIFY_PROMPT,
    LEAD_SAVED_TEMPLATE,
)

logger = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.1-8b-instant"
_AUTO_NUDGE_AFTER = 3

# Words that are NOT valid names
_NOT_A_NAME = {
    "hello", "hi", "hey", "hii", "hiii", "namaste", "yo", "yes", "no", "ok",
    "okay", "sup", "start", "none", "test", "bot", "riya", "agent", "help",
    "nope", "yep", "nah", "yeah", "sure", "fine", "good", "great", "nice",
    "skip", "s", "next", "done", "begin", "go", "search",
}
_PHONE_RE = re.compile(r'^(?:\+91[-\s]?)?[6-9]\d{9}$')

# Keywords signalling a property search (used for lead_capture escape hatch)
_SEARCH_RE = re.compile(
    r"\b(show|properties|flats?|houses?|apartments?|bhk|budget|area|lakh|crore|crores|"
    r"available|search|find|looking|options|listings?|rooms?|bedroom|furnish)\b",
    re.IGNORECASE,
)

# Keywords signalling shortlist intent
_SHORTLIST_RE = re.compile(
    r"\b(shortlist|saved?|favorites?|liked|my properties|show saved|bookmark)\b",
    re.IGNORECASE,
)

# Strong action words — buyer wants to act NOW (visit/book), not just discuss
_ACTION_RE = re.compile(
    r"\b(visit|book|schedule|arrange|contact|call me|broker|proceed|"
    r"i'?ll take|interested in (buying|booking)|site visit|see it in person|"
    r"meet|appointment|finalize|go ahead)\b",
    re.IGNORECASE,
)

# "compare 1 and 2" / "which is better/cheaper" / "tell me about property 3" / "difference"
_COMPARE_RE = re.compile(
    r"\b(compare|difference between|vs\.?|versus|"
    r"which (is|one is|one|are)?\s*(better|best|good|cheaper|cheapest|cheap|"
    r"bigger|biggest|larger|largest|smaller|expensive|nicer|closer|newer)|"
    r"tell me (more )?about (property |option |number |the )?(\d|first|second|third)|"
    r"more (details?|info) (on|about) (property |option |number |the )?(\d|first|second|third)|"
    r"property \d|option \d|(first|second|third) one)\b",
    re.IGNORECASE,
)

# "show more / other options" — exclude already-shown properties
_MORE_OPTIONS_RE = re.compile(
    r"\b(more options|more properties|show more|other options|different options|"
    r"anything else|what else|more listings|see more|more results|any other|"
    r"more choices|other properties|something else|different properties)\b",
    re.IGNORECASE,
)

# "more areas / different location" — clear area filter so all of Lucknow is searched
_DIFF_AREA_RE = re.compile(
    r"\b(more areas?|other areas?|different areas?|another area|different location|"
    r"other locations?|change area|somewhere else|other parts?|any areas?|"
    r"broader search|anywhere in lucknow|expand search|all areas?|"
    r"across lucknow|whole lucknow|entire lucknow|"
    r"anything.{0,20}near|any propert\w*.{0,12}near|"
    r"show.{0,12}near|find.{0,12}near|"
    r"properties? anywhere|flats? anywhere|houses? anywhere)\b",
    re.IGNORECASE,
)

# "remove budget" / "no budget limit" — clear budget filters
_CLEAR_BUDGET_RE = re.compile(
    r"\b(remove.{0,10}budget|no budget|any budget|any price|ignore budget|"
    r"without budget|price.{0,10}(doesn'?t matter|not important|don'?t care)|"
    r"budget.{0,10}(doesn'?t matter|not important|remove|ignore)|any amount|no limit)\b",
    re.IGNORECASE,
)

# "any BHK" / "doesn't matter" — clear BHK + property_type filters
_ANY_BHK_RE = re.compile(
    r"\b(any bhk|any type|not.{0,8}(specific|particular).{0,8}bhk|bhk.{0,10}doesn'?t matter|"
    r"any flat|any house|any property|any configuration|don'?t care.{0,10}bhk|"
    r"not.{0,5}(2|3|4) bhk|whatever type|any size|"
    r"any.{0,8}(would|will|is|are)?.{0,5}(work|fine|good|ok|okay|do)|"
    r"anything.{0,6}(works?|fine|good|ok)|doesn'?t matter|"
    r"no preference|whatever.{0,6}(works?|you have|is available)|"
    r"(you|u).{0,6}(decide|choose|suggest|recommend)|open to any)\b",
    re.IGNORECASE,
)


def _mentions_known_area(message: str) -> bool:
    """True if the message contains a recognised Lucknow neighbourhood name."""
    from agent.intent_extractor import _LUCKNOW_AREAS
    msg = message.lower().replace(" ", "")
    for area in _LUCKNOW_AREAS:
        if area == "lucknow":
            continue
        if area.replace(" ", "") in msg:
            return True
    return False


def _grounded_in_message(value: str | None, message: str) -> bool:
    """True if the extracted value actually appears in the user's message (not history)."""
    if not value:
        return False
    return value.lower().replace(" ", "") in message.lower().replace(" ", "")


def _resolve_location_switch(
    conv: ConversationManager,
    extracted: dict,
    user_message: str,
    is_diff_area: bool,
) -> None:
    """
    Treat area / named_landmark / nearby as a single location group and rebuild it
    from what THIS message actually says, so stale anchors can't leak forward.

    Rules (only fire when the message carries a fresh location signal):
      • New explicit AREA   → that area becomes the anchor; drop any stale landmark.
      • New explicit LANDMARK (and no area named) → landmark is the anchor; drop area.
      • Broadening ("anything near X") → drop area + stale landmark; keep new nearby.
      • nearby is replaced by whatever this message specified (drops stale nearby).
      • No location signal at all → leave the whole group untouched (pure refinement).
    Only values literally present in the current message count — history re-extraction
    by the LLM is ignored, mirroring the sticky-clear philosophy.
    """
    ext_area     = extracted.get("area")     if _grounded_in_message(extracted.get("area"), user_message) else None
    ext_landmark = extracted.get("named_landmark") if _grounded_in_message(extracted.get("named_landmark"), user_message) else None
    ext_nearby   = extracted.get("nearby") or []

    has_location_signal = bool(ext_area or ext_landmark or ext_nearby or is_diff_area)
    if not has_location_signal:
        return  # non-location refinement (budget/BHK/etc.) — keep location as-is

    # ── Area anchor ──
    if ext_area:
        conv.requirements["area"] = ext_area
    elif is_diff_area or (ext_landmark and not ext_area):
        # broadening, or a landmark with no area → the old area no longer applies
        conv.requirements["area"] = None

    # ── Landmark anchor ── (replace; None drops a stale landmark)
    if ext_landmark:
        conv.requirements["named_landmark"] = ext_landmark
        conv.requirements["named_landmark_max_km"] = extracted.get("named_landmark_max_km") or 3.0
    else:
        conv.requirements["named_landmark"] = None
        conv.requirements["named_landmark_max_km"] = None

    # ── Nearby ── (replace with this message's list; drops stale connectivity tags)
    conv.requirements["nearby"] = ext_nearby


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


def _sys(user_name: str | None = None) -> str:
    if user_name:
        return SYSTEM_PROMPT_NAMED.format(name=user_name)
    return SYSTEM_PROMPT


def _get_user_name(conv: ConversationManager) -> str | None:
    return (conv.requirements.get("_profile") or {}).get("name")


# ── Web onboarding (name + phone, same flow as Telegram) ─────────────────────

def _handle_web_onboarding(conv: ConversationManager, user_message: str) -> dict | None:
    """
    For web platform: collect name then phone before property search.
    Returns response dict if still in onboarding (or for __init__ reload),
    otherwise None so normal routing continues.

    NOTE: messages handled here are NOT added to conversation history so they
    don't confuse the intent extractor (phone number in history → wrong "strong" intent).
    """
    profile = conv.requirements.get("_profile") or {}
    step = profile.get("onboarding_step")

    # Auto-init: page load / reload — handle here so __init__ never hits intent extractor
    if user_message == "__init__":
        if step == "complete":
            name = profile.get("name")
            # Reset all search requirements on each session start so the bot asks
            # qualifying questions fresh instead of using stale session data.
            # Keep only _profile (name, phone) — discard old budget/area/BHK/flags.
            conv.requirements = {"_profile": profile}
            conv.set_stage("discovery")
            if name:
                return {"reply": f"Welcome back, {name}! What are you looking for today?", "properties": []}
            return {"reply": "Hello! I'm Riya, your property consultant for Lucknow. What are you looking for?", "properties": []}
        # Not yet onboarded — fall through to start onboarding
        step = None

    if step == "complete":
        return None  # done, proceed to normal routing

    text = user_message.strip()

    # First message — start onboarding
    if not step:
        profile["onboarding_step"] = "waiting_name"
        conv.requirements["_profile"] = profile
        return {"reply": "Before we start, what should I call you?", "properties": []}

    # Waiting for name
    if step == "waiting_name":
        alpha = sum(1 for c in text if c.isalpha())
        has_digits = any(c.isdigit() for c in text)
        if alpha >= 2 and not has_digits and text.lower() not in _NOT_A_NAME:
            name = text.title()
            profile["name"] = name
            profile["onboarding_step"] = "waiting_phone"
            conv.requirements["_profile"] = profile
            return {
                "reply": (
                    f"Nice to meet you, {name}! Could I get your contact number? "
                    "I'll only use it when you're ready to schedule a visit. "
                    "(Type _skip_ to skip)"
                ),
                "properties": [],
            }
        return {"reply": "Please enter your name to get started.", "properties": []}

    # Waiting for phone
    if step == "waiting_phone":
        if text.lower() in ("skip", "s", "no", "nope", "later", "next"):
            profile["onboarding_step"] = "complete"
            conv.requirements["_profile"] = profile
            name = profile.get("name", "")
            return {
                "reply": (
                    f"No problem, {name}! So, what kind of property are you looking for? "
                    "Tell me your budget and I'll get started."
                ),
                "properties": [],
            }
        cleaned = re.sub(r"[\s\-()]", "", text)
        if _PHONE_RE.match(cleaned) or re.match(r"^[6-9]\d{9}$", cleaned):
            phone = cleaned.lstrip("+91").lstrip("91") if len(cleaned) > 10 else cleaned
            profile["phone"] = phone
            profile["onboarding_step"] = "complete"
            conv.requirements["_profile"] = profile
            name = profile.get("name", "")
            return {
                "reply": (
                    f"Got it, {name}! Now what kind of property are you looking for? "
                    "Tell me your budget and I'll get started."
                ),
                "properties": [],
            }
        return {
            "reply": "Please enter a valid 10-digit Indian mobile number, or type _skip_.",
            "properties": [],
        }

    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def process_message(session_id: str, user_message: str, platform: str = "web") -> dict:
    """
    Process one user message.
    Returns {"reply": str, "properties": list[dict]}.

    Web onboarding messages (name/phone) are intentionally NOT added to chat history
    so they don't confuse the LLM intent extractor (e.g. a phone number in history
    should not trigger lead_intent = "strong").
    """
    conv = ConversationManager(session_id, platform)
    conv.load()

    # Web onboarding: handle BEFORE adding to history
    if platform == "web":
        onboarding_result = _handle_web_onboarding(conv, user_message)
        if onboarding_result is not None:
            conv.save()
            return onboarding_result

    # Normal conversation — add to history and route
    conv.add_user_message(user_message)
    reply, properties = _route(conv, user_message)
    conv.add_assistant_message(reply)
    conv.save()
    return {"reply": reply, "properties": properties}


_NOISE_RE = re.compile(r"^[\W\d\s]{1,4}$")


def _route(conv: ConversationManager, user_message: str) -> tuple[str, list]:
    user_name = _get_user_name(conv)

    # ── Shortlist request ─────────────────────────────────────────────────────
    if _SHORTLIST_RE.search(user_message):
        return _show_shortlist(conv, user_name)

    # ── Compare / detail request about already-shown properties ───────────────
    # Only if we've shown properties AND the user isn't trying to act (visit/book) or
    # in lead capture. A message like "I love the first one, can I visit?" must fall
    # through to intent extraction → strong intent → lead capture, NOT comparison.
    if (conv.requirements.get("_last_shown_text")
            and _COMPARE_RE.search(user_message)
            and not _ACTION_RE.search(user_message)
            and not conv.is_lead_capture_stage()):
        return _compare_properties(conv, user_message, user_name), conv.requirements.get("_last_shown_cards", [])

    # ── Stage: collecting name + phone ───────────────────────────────────────
    if conv.is_lead_capture_stage():
        if _SEARCH_RE.search(user_message):
            conv.set_stage("discovery")  # escape hatch
        else:
            return _handle_lead_capture(conv, user_message, user_name), []

    # ── After completed lead, allow new search (post_lead cooldown) ──────────
    if conv.stage in ("done", "post_lead"):
        remaining = conv.requirements.get("_post_lead_turns", 0)
        if remaining > 0:
            conv.requirements["_post_lead_turns"] = remaining - 1
        else:
            conv.set_stage("discovery")

    # ── Guard: ignore very short/noise messages mid-conversation ─────────────
    stripped = user_message.strip()
    if _NOISE_RE.match(stripped) and conv.stage == "recommending":
        return "Did any of those properties interest you? Let me know if you'd like details on any of them.", []

    # ── Detect filter-modifying intents before extraction ────────────────────
    is_more_request = bool(_MORE_OPTIONS_RE.search(user_message))
    is_diff_area    = bool(_DIFF_AREA_RE.search(user_message))
    clear_budget    = bool(_CLEAR_BUDGET_RE.search(user_message))
    clear_bhk       = bool(_ANY_BHK_RE.search(user_message))

    # If the user named a specific area in THIS message (e.g. "anything near metro in
    # Alambagh"), a broadening keyword must NOT wipe that area — they want to keep it.
    if is_diff_area and _mentions_known_area(user_message):
        is_diff_area = False

    # Set sticky-clear flags BEFORE merge so merge doesn't overwrite them
    if is_diff_area:
        conv.requirements["area"] = None
        conv.requirements["_area_cleared"] = True
    if clear_budget:
        conv.requirements["max_budget_cr"] = None
        conv.requirements["min_budget_cr"] = None
        conv.requirements["_budget_cleared"] = True
    if clear_bhk:
        conv.requirements["bhk"] = None
        conv.requirements["property_type"] = None
        conv.requirements["_bhk_cleared"] = True

    # When user changes search params, reset nudge counter AND shown_ids for fresh results
    if clear_budget or clear_bhk or is_diff_area:
        conv._recommendation_count = 0
        conv.requirements["_shown_ids"] = []

    # ── Extract intent ────────────────────────────────────────────────────────
    # Snapshot key filters BEFORE merge so we can detect what the user changed.
    prev_nearby = list(conv.requirements.get("nearby") or [])
    prev_bhk    = conv.requirements.get("bhk")
    prev_budget = conv.requirements.get("max_budget_cr")
    prev_area   = conv.requirements.get("area")

    extracted = extract_intent(user_message, conv.get_history_for_llm())
    conv.requirements = merge_requirements(conv.requirements, extracted)
    lead_level = extracted.get("lead_intent_level", "none")

    # If LLM extraction completely failed, ask a clarifying question rather than searching blind
    if extracted.get("_extraction_failed"):
        return _clarify(conv, user_message, user_name), []

    # Reset shown_ids whenever any core search criterion changes — so a new search
    # context (different BHK, budget, area, or nearby place) returns fresh results
    # instead of being filtered against the previously-shown set.
    new_nearby = conv.requirements.get("nearby") or []
    criteria_changed = (
        (new_nearby and new_nearby != prev_nearby)
        or conv.requirements.get("bhk") != prev_bhk
        or conv.requirements.get("max_budget_cr") != prev_budget
        or conv.requirements.get("area") != prev_area
    )
    if criteria_changed:
        conv.requirements["_shown_ids"] = []
        conv._recommendation_count = 0

    # ── Location-context switch: drop stale location anchors ──────────────────
    # area / named_landmark / nearby form ONE location group. When the user moves to
    # a new location anchor in THIS message, any previously-stored anchor they did NOT
    # reassert must be dropped — otherwise a stale landmark/nearby silently filters out
    # every later search (e.g. "near Sahara Hospital" → then "what about Gomti Nagar"
    # would keep hunting near Sahara Hospital and return nothing).
    _resolve_location_switch(conv, extracted, user_message, is_diff_area)

    # ── Sticky-clear enforcement ──────────────────────────────────────────────
    # The intent extractor sees full chat history and may re-extract values the user
    # intentionally cleared. Locks are lifted ONLY when the CURRENT message explicitly
    # contains the relevant keyword — not just because LLaMA saw it in history.

    _budget_kw = re.compile(r'\b\d[\d,]*\s*(lakh|lac|lakhs|lacs|crore|cr|crores)\b', re.IGNORECASE)
    _bhk_kw    = re.compile(r'\b(\d\s*bhk|bhk\s*\d|flat|house|villa|apartment|bungalow|plot)\b', re.IGNORECASE)

    if is_diff_area:
        conv.requirements["area"] = None  # force even if LLM re-extracted from history
    elif conv.requirements.get("_area_cleared"):
        new_area = extracted.get("area")
        if new_area and new_area.lower().replace(" ", "") in user_message.lower().replace(" ", ""):
            conv.requirements["_area_cleared"] = False  # user named a real area in this message
        else:
            conv.requirements["area"] = None  # keep cleared (history-based re-extraction)

    if clear_budget:
        conv.requirements["max_budget_cr"] = None
        conv.requirements["min_budget_cr"] = None
    elif conv.requirements.get("_budget_cleared"):
        new_budget = extracted.get("max_budget_cr") or extracted.get("min_budget_cr")
        if new_budget and _budget_kw.search(user_message):
            conv.requirements["_budget_cleared"] = False  # user gave explicit new budget
        else:
            conv.requirements["max_budget_cr"] = None
            conv.requirements["min_budget_cr"] = None

    if clear_bhk:
        conv.requirements["bhk"] = None
        conv.requirements["property_type"] = None
    elif conv.requirements.get("_bhk_cleared"):
        new_bhk = extracted.get("bhk")
        new_type = extracted.get("property_type")
        if (new_bhk or new_type) and _bhk_kw.search(user_message):
            conv.requirements["_bhk_cleared"] = False  # user explicitly named a type/BHK
        else:
            conv.requirements["bhk"] = None
            conv.requirements["property_type"] = None

    # ── Strong intent -> move to lead capture ─────────────────────────────────
    if lead_level == "strong":
        conv.set_stage("lead_capture")
        return _ask_for_contact(conv, user_name), []

    # ── "More options" while in recommending stage → exclude already-shown ───
    if is_more_request and conv.stage == "recommending":
        return _recommend(conv, user_message, lead_level, user_name, is_more_request=True)

    # ── Decide whether to search or keep clarifying ───────────────────────────
    budget_cleared = conv.requirements.get("_budget_cleared", False)
    bhk_cleared    = conv.requirements.get("_bhk_cleared", False)
    area_cleared   = conv.requirements.get("_area_cleared", False)
    # A landmark ("near Phoenix Mall") or a nearby place ("near metro") counts as a
    # location anchor just like a named area — so the live distance search can fire.
    has_location   = bool(conv.requirements.get("area")
                          or conv.requirements.get("named_landmark")
                          or conv.requirements.get("nearby"))
    has_bhk_or_type = bool(conv.requirements.get("bhk") or conv.requirements.get("property_type"))

    # Allow search when: (budget + location + bhk/type) OR clear-flags relax those constraints
    can_search = conv.has_enough_info() or (
        (has_location or area_cleared) and (has_bhk_or_type or bhk_cleared) and (conv.requirements.get("max_budget_cr") or budget_cleared)
    )

    if can_search:
        conv.set_stage("recommending")
        return _recommend(conv, user_message, lead_level, user_name)
    else:
        return _clarify(conv, user_message, user_name), []


def _build_search_query(requirements: dict, user_message: str) -> str:
    """
    Build a rich semantic query for pgvector cosine search.
    Includes type synonyms so the embedding matches diverse property descriptions.
    """
    parts = []
    bhk = requirements.get("bhk")
    ptype = requirements.get("property_type") or ""

    type_syns = {
        "flat": "flat apartment home",
        "house": "house bungalow independent home",
        "villa": "villa bungalow luxury home independent house",
        "plot": "plot land open",
        "shop": "shop commercial space",
    }

    if bhk:
        syn = type_syns.get(ptype.lower(), "property home")
        parts.append(f"{bhk} BHK {syn}")
    elif ptype:
        # type without BHK — still use full synonyms so vector matches well
        syn = type_syns.get(ptype.lower(), ptype)
        parts.append(syn)
    else:
        parts.append("residential property flat house apartment home")  # broad when no BHK/type

    if requirements.get("area"):
        parts.append(f"in {requirements['area']} Lucknow")
    elif requirements.get("city"):
        parts.append(f"in {requirements['city']}")

    if requirements.get("max_budget_cr"):
        cr = requirements["max_budget_cr"]
        lakh = cr * 100
        if lakh == int(lakh):
            parts.append(f"affordable under {int(lakh)} lakh rupees")
        else:
            parts.append(f"under {cr:.2f} crore")

    if requirements.get("nearby"):
        parts.append(f"near {' '.join(requirements['nearby'])}")
    if requirements.get("furnishing"):
        parts.append(requirements["furnishing"])
    if requirements.get("amenities"):
        parts.append(f"with {' '.join(requirements['amenities'][:3])}")

    return " ".join(parts) if parts else user_message


def _recommend(
    conv: ConversationManager,
    user_message: str,
    lead_level: str,
    user_name: str | None,
    is_more_request: bool = False,
) -> tuple[str, list]:
    from rag.retriever import to_card

    shown_ids: list = conv.requirements.get("_shown_ids") or []
    # Only exclude already-shown IDs when user explicitly asks for MORE options.
    # For any fresh "show me properties in X" request, always search clean.
    exclude = shown_ids if is_more_request else []

    search_query = _build_search_query(conv.requirements, user_message)
    req = conv.requirements
    filter_note = ""  # will be injected into LLM prompt if we relaxed filters

    properties = retrieve(search_query, req, top_k=5, exclude_ids=exclude)

    # ── Named landmark not found (geocoding failed) — HIGHEST priority note ────
    # Check first so the buyer is told honestly we couldn't locate their landmark,
    # before any area/type mismatch messaging.
    if properties and req.get("named_landmark"):
        lm_not_found = next(
            (p.get("named_landmark_not_found") for p in properties if p.get("named_landmark_not_found")),
            None,
        )
        if lm_not_found:
            filter_note = (
                f"(I couldn't pinpoint '{lm_not_found}' on the map to measure exact distances — "
                f"showing the best-matched properties instead; mention this honestly and suggest "
                f"they confirm the exact location with our consultant)"
            )

    # ── Progressive fallback when user insists on an area but no results ──────
    area = req.get("area")
    if not properties and area:
        # Fallback 1: remove BHK + property_type, keep area + budget
        req_f1 = {**req, "bhk": None, "property_type": None}
        properties = retrieve(search_query, req_f1, top_k=5, exclude_ids=exclude)
        if properties:
            filter_note = f"(I widened the search to include all BHK types in {area})"

    if not properties and area:
        # Fallback 2: also remove budget, keep area only
        req_f2 = {**req, "bhk": None, "property_type": None, "max_budget_cr": None, "min_budget_cr": None}
        properties = retrieve(search_query, req_f2, top_k=5, exclude_ids=exclude)
        if properties:
            filter_note = f"(I've shown all available in {area} — some may be above your stated budget)"

    # ── Detect area mismatch ──────────────────────────────────────────────────
    if area and properties and not filter_note:
        req_area_norm = area.lower().replace(" ", "")
        prop_areas = [(p.get("area") or "").lower().replace(" ", "") for p in properties]
        any_match = any(req_area_norm in pa or pa in req_area_norm for pa in prop_areas)
        if not any_match:
            shown_areas = sorted(set((p.get("area") or "").title() for p in properties if p.get("area")))
            alt_text = " and ".join(shown_areas[:2]) if shown_areas else "nearby areas"
            filter_note = (
                f"(No properties currently listed in {area} — showing best available options "
                f"from {alt_text} instead; mention this honestly to the buyer)"
            )

    # ── Detect type mismatch (e.g. user asked for villas, DB has only flats) ──
    req_type = (req.get("property_type") or "").lower().strip()
    if req_type and properties and not filter_note:
        _VILLA_GRP = {"villa", "house", "bungalow", "independent house", "independent"}
        _FLAT_GRP = {"flat", "apartment", "builder floor", "builder's floor", "floor"}

        def _same_type_group(t1: str, t2: str) -> bool:
            if t1 == t2: return True
            if t1 in _VILLA_GRP and t2 in _VILLA_GRP: return True
            if t1 in _FLAT_GRP and t2 in _FLAT_GRP: return True
            return False

        prop_types = [(p.get("property_type") or "").lower() for p in properties]
        if not any(_same_type_group(req_type, pt) for pt in prop_types):
            shown_types = sorted(set(pt.title() for pt in prop_types if pt))
            alt = "/".join(shown_types[:2]) if shown_types else "available properties"
            filter_note = (
                f"(No {req_type}s available with the current filters — "
                f"showing {alt} as the closest alternatives; acknowledge this to the buyer)"
            )

    # ── Detect amenity mismatch (e.g. "with lift" but no property has lifts) ──
    # NOTE: only check true amenities. Connectivity items (metro/hospital/school/etc.)
    # come via `nearby` and are handled by distance filtering in the retriever — they are
    # NOT listed in a property's amenities, so checking them here gives false negatives.
    _CONNECTIVITY_WORDS = {"metro", "metro station", "railway", "railway station", "station",
                            "hospital", "school", "market", "bus", "bus stop", "airport",
                            "park", "mall", "college", "highway"}
    req_amenities = [
        a.lower() for a in (req.get("amenities") or [])
        if a.lower() not in _CONNECTIVITY_WORDS
    ]
    if req_amenities and properties and not filter_note:
        # Check if any requested amenity appears in any returned property
        _AMENITY_SYNONYMS = {
            "lift": ["lift", "elevator", "lifts"],
            "gym": ["gym", "gymnasium", "fitness"],
            "pool": ["pool", "swimming"],
            "parking": ["parking", "car park"],
        }
        for req_am in req_amenities:
            synonyms = _AMENITY_SYNONYMS.get(req_am, [req_am])
            prop_amenity_strs = [
                " ".join(p.get("top_amenities") or []).lower() for p in properties
            ]
            if not any(syn in pam for syn in synonyms for pam in prop_amenity_strs):
                filter_note = (
                    f"(None of the available properties have '{req_am}' in their listed amenities; "
                    f"let the buyer know but still describe the properties shown)"
                )
                break

    # ── "More options" exhausted — suggest alternatives ───────────────────────
    if not properties and is_more_request:
        return _suggest_alternatives(conv, user_name), []

    if properties:
        new_ids = [p.get("id") for p in properties if p.get("id")]
        conv.requirements["_shown_ids"] = list(dict.fromkeys(shown_ids + new_ids))

        cards = [to_card(p) for p in properties]
        # Remember the most-recent visible set so the user can ask
        # "which is better, 1 or 2?" / "tell me about property 3" afterwards.
        conv.requirements["_last_shown_text"] = format_properties_for_llm(properties)
        conv.requirements["_last_shown_cards"] = cards
        props_text = conv.requirements["_last_shown_text"]
        avail_note = f"\n⚠️ availability_note: {filter_note}\n" if filter_note else ""
        user_prompt = PROPERTY_RECOMMENDATION_PROMPT.format(
            count=len(properties),
            requirements=_requirements_summary(req),
            properties_text=props_text,
            availability_note=avail_note,
        )
    else:
        cards = []
        user_prompt = NO_RESULTS_PROMPT.format(requirements=_requirements_summary(req))

    messages = [{"role": "system", "content": _sys(user_name)}]
    messages += conv.get_history_for_llm()
    messages.append({"role": "user", "content": user_prompt})
    reply = _llm(messages, max_tokens=300)

    if lead_level == "soft" and properties:
        conv.requirements["_liked_property_id"] = properties[0].get("id", "")

    rec_count = conv.get_recommendation_count()
    should_nudge = (lead_level == "soft") or (rec_count >= _AUTO_NUDGE_AFTER and lead_level == "none")
    if should_nudge and properties:
        conv.increment_recommendation_count()
        nudge_prompt = SOFT_INTEREST_PROMPT.format(
            context=_requirements_summary(req),
            message=user_message,
        )
        nudge_messages = [{"role": "system", "content": _sys(user_name)}]
        nudge_messages.append({"role": "assistant", "content": reply})
        nudge_messages.append({"role": "user", "content": nudge_prompt})
        reply = _llm(nudge_messages, temperature=0.5, max_tokens=150)
    else:
        conv.increment_recommendation_count()

    return reply, cards


def _suggest_alternatives(conv: ConversationManager, user_name: str | None) -> str:
    """
    Called when user asks for more options but we've shown everything matching.
    Suggests: different area, relaxed budget, different BHK.
    """
    r = conv.requirements
    area = r.get("area", "this area")
    budget_cr = r.get("max_budget_cr")
    bhk = r.get("bhk")

    # Build suggestions
    suggestions = []
    if area and area.lower() != "lucknow":
        suggestions.append("explore a nearby area like Gomti Nagar, Aliganj, or Indiranagar")
    if budget_cr:
        stretched = round(budget_cr * 1.3, 2)
        lakh = stretched * 100
        budget_label = f"Rs.{int(lakh)} lakh" if lakh == int(lakh) else f"Rs.{stretched:.2f} crore"
        suggestions.append(f"stretch the budget slightly to around {budget_label}")
    if bhk and bhk > 1:
        suggestions.append(f"consider {bhk - 1} BHK options which are more available")

    if not suggestions:
        suggestions = ["try a different area", "adjust the budget range"]

    suggestion_text = ", or ".join(suggestions[:2])

    name_part = f"{user_name}, " if user_name else ""
    messages = [{"role": "system", "content": _sys(user_name)}]
    messages.append({
        "role": "user",
        "content": (
            f"The buyer ({name_part}searched: {_requirements_summary(r)}) has seen all available properties. "
            f"As Riya, write 2 sentences: (1) acknowledge no new matches right now, "
            f"(2) suggest ONE of: {suggestion_text}. "
            "Professional English, warm and helpful."
        ),
    })
    return _llm(messages, temperature=0.6, max_tokens=120)


def _handle_lead_capture(
    conv: ConversationManager,
    user_message: str,
    user_name: str | None,
) -> str:
    name, phone = extract_name_and_phone(user_message)

    if name and phone:
        liked_id = conv.requirements.get("_liked_property_id")
        lead = create_lead(
            session_id=conv.session_id,
            requirements=conv.requirements,
            name=name,
            phone=phone,
            property_id=liked_id,
        )
        if lead:
            notify_broker_via_n8n(lead, conv.requirements)
            profile = conv.requirements.get("_profile") or {}
            profile["name"] = name
            profile["phone"] = phone
            conv.requirements["_profile"] = profile
            conv.set_stage("post_lead")
            conv.requirements["_post_lead_turns"] = 3
            return LEAD_SAVED_TEMPLATE.format(name=name, phone=phone)
        else:
            return "Apologies, something went wrong. Could you share your name and number once more?"

    if phone:
        return "Got your number! Could you also share your name? Our consultant would like to address you properly."

    messages = [{"role": "system", "content": _sys(user_name)}]
    messages += conv.get_history_for_llm()
    messages.append({
        "role": "user",
        "content": (
            "The buyer wants to proceed but hasn't shared their name and phone yet. "
            "Gently ask again — one warm, professional sentence."
        ),
    })
    return _llm(messages, temperature=0.5, max_tokens=120)


def _ask_for_contact(conv: ConversationManager, user_name: str | None) -> str:
    messages = [{"role": "system", "content": _sys(user_name)}]
    messages += conv.get_history_for_llm()
    messages.append({
        "role": "user",
        "content": LEAD_CAPTURE_PROMPT.format(
            context=_requirements_summary(conv.requirements)
        ),
    })
    return _llm(messages, temperature=0.5, max_tokens=150)


def _clarify(
    conv: ConversationManager,
    user_message: str,
    user_name: str | None,
) -> str:
    """
    Code determines WHAT to ask (guaranteed correct order).
    LLM only handles phrasing — cannot ask the wrong thing.
    """
    r = conv.requirements
    has_budget = bool(r.get("max_budget_cr") or r.get("min_budget_cr"))
    has_area = bool(r.get("area"))
    has_bhk = bool(r.get("bhk"))
    has_type = bool(r.get("property_type"))
    budget_cleared = r.get("_budget_cleared", False)
    bhk_cleared = r.get("_bhk_cleared", False)
    area_cleared = r.get("_area_cleared", False)

    # Format current budget for LLM context
    if r.get("max_budget_cr"):
        cr = r["max_budget_cr"]
        lakh = cr * 100
        budget_label = f"Rs.{int(lakh)} lakh" if lakh == int(lakh) else f"Rs.{cr:.2f} crore"
    elif r.get("min_budget_cr"):
        cr = r["min_budget_cr"]
        lakh = cr * 100
        budget_label = f"above Rs.{int(lakh)} lakh" if lakh == int(lakh) else f"above Rs.{cr:.2f} crore"
    else:
        budget_label = "open (no limit)" if budget_cleared else None

    area = r.get("area", "")

    # Only ask for budget if it hasn't been intentionally cleared
    if not has_budget and not budget_cleared:
        ask_instruction = (
            "Ask what budget range they are working with. "
            "Be natural — e.g. 'What budget are you working with?' or 'What price range are you considering?'"
        )
    elif not has_area and not area_cleared:
        ask_instruction = (
            "Ask which area or neighbourhood in Lucknow they prefer. "
            "Give 2-3 options as examples: Gomti Nagar, Aliganj, Hazratganj, Indiranagar, etc."
        )
    elif not has_bhk and not has_type and not bhk_cleared:
        ask_instruction = (
            f"Area ({area}) is known. "
            "Ask what TYPE of property they want AND how many bedrooms in one natural question. "
            "Example: 'Are you looking for a 2 BHK flat, 3 BHK house, or maybe a villa?' "
            "This gets both property type and BHK in one go."
        )
    elif not has_bhk and not bhk_cleared:
        ask_instruction = "Ask how many bedrooms — 1 BHK, 2 BHK, or 3 BHK."
    elif not has_type and not bhk_cleared:
        ask_instruction = "Ask if they prefer a flat, independent house, or villa."
    else:
        ask_instruction = (
            "Ask if they have any specific requirements: furnished vs unfurnished, "
            "near metro/school/hospital, parking, floor preference, or any other amenity."
        )

    messages = [{"role": "system", "content": _sys(user_name)}]
    messages += conv.get_history_for_llm()
    messages.append({
        "role": "user",
        "content": (
            f"Buyer said: \"{user_message}\"\n\n"
            f"Your task: {ask_instruction}\n\n"
            "Write ONE sentence only. Warm and natural. Do not ask for anything else."
        ),
    })
    return _llm(messages, temperature=0.6, max_tokens=80)


def _compare_properties(
    conv: ConversationManager,
    user_message: str,
    user_name: str | None,
) -> str:
    """
    Answer a comparison or detail question ("which is better 1 or 2?",
    "tell me about property 3") using the properties most recently shown.
    Grounded strictly in the stored property summary — never invents data.
    """
    shown_text = conv.requirements.get("_last_shown_text", "")
    messages = [{"role": "system", "content": _sys(user_name)}]
    messages.append({
        "role": "user",
        "content": (
            f"The buyer is asking about properties I just showed them.\n"
            f"Their question: \"{user_message}\"\n\n"
            f"The properties currently on screen:\n{shown_text}\n\n"
            "As Riya, answer their question helpfully in 2-4 sentences. If they're comparing, "
            "point out the key practical differences (price, size, BHK, location, standout amenity, "
            "or proximity) and give a genuine recommendation based on the data. If they want detail on "
            "one property, describe it warmly. End by asking if they'd like to visit it.\n\n"
            "⚠️ Use ONLY the facts in the summary above — never invent prices, amenities, or features."
        ),
    })
    return _llm(messages, temperature=0.5, max_tokens=220)


def _show_shortlist(conv: ConversationManager, user_name: str | None) -> tuple[str, list]:
    """Return the user's saved/shortlisted properties."""
    from rag.retriever import to_card
    from database.supabase_client import get_client

    shortlist = conv.requirements.get("_shortlist", [])
    if not shortlist:
        name_part = f", {user_name}" if user_name else ""
        return (
            f"You haven't saved any properties yet{name_part}. "
            "When you see one you like, click 'Save ❤️' to add it to your shortlist!",
            [],
        )

    try:
        client = get_client()
        result = client.table("properties").select("*").in_("id", shortlist).execute()
        if not result.data:
            return "I couldn't find your saved properties. They may no longer be available.", []
        cards = [
            to_card({"id": r["id"], "data": r["data"], "score": 0, "similarity": 1.0})
            for r in result.data
        ]
        name_part = f"{user_name}, you have" if user_name else "You have"
        return (
            f"{name_part} {len(cards)} saved propert{'y' if len(cards) == 1 else 'ies'}. Here they are!",
            cards,
        )
    except Exception as e:
        logger.error(f"Shortlist fetch error: {e}")
        return "I had trouble loading your saved properties. Please try again.", []


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
