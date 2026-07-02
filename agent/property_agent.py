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

from agent.conversation_manager import ConversationManager
from agent.intent_extractor import extract_intent, merge_requirements
from agent.lead_collector import extract_name_and_phone, create_lead, notify_broker_via_n8n
from rag.retriever import retrieve, format_properties_for_llm
from rag.prompts import (
    SYSTEM_PROMPT, SYSTEM_PROMPT_NAMED,
    PROPERTY_RECOMMENDATION_PROMPT, LEAD_CAPTURE_PROMPT,
    NO_RESULTS_PROMPT, CLARIFY_PROMPT,
    LEAD_SAVED_TEMPLATE, ASK_VISIT_TIME_TEMPLATE, VISIT_SCHEDULED_TEMPLATE,
    VISIT_SKIP_TEMPLATE,
)

logger = logging.getLogger(__name__)

_AUTO_NUDGE_AFTER = 3
# Beyond this straight-line distance, a property is NOT honestly "near" a named landmark.
_LANDMARK_NEAR_KM = 2.5

# Users are in Lucknow (IST) but the server runs in UTC. All visit times must be
# interpreted/stored in IST, else "5 pm" becomes 5 pm UTC (= 10:30 pm IST) on the
# broker dashboard and Google Calendar.
from zoneinfo import ZoneInfo
IST = ZoneInfo("Asia/Kolkata")

# Words that are NOT valid names
_NOT_A_NAME = {
    "hello", "hi", "hey", "hii", "hiii", "namaste", "yo", "yes", "no", "ok",
    "okay", "sup", "start", "none", "test", "bot", "riya", "agent", "help",
    "nope", "yep", "nah", "yeah", "sure", "fine", "good", "great", "nice",
    "skip", "s", "next", "done", "begin", "go", "search",
}
_PHONE_RE = re.compile(r'^(?:\+91[-\s]?)?[6-9]\d{9}$')

# Inappropriate / abusive / off-topic-harassment input — decline politely and redirect.
# Word-boundaried to avoid false hits on legitimate property vocabulary.
_INAPPROPRIATE_RE = re.compile(
    r"\b(sex|sexual|sexy|sext\w*|horny|nudes?|naked|porn|p[o0]rn\w*|"
    r"f+u+c+k+\w*|f\*+ck\w*|bsdk|chut[iya]\w*|gaand|lund|lawda|randi|madarchod|behenchod|"
    r"d[i1]ck|pen[i1]s|vag[i1]na|pussy|boobs?|t[i1]ts|cum|blowjob|hand ?job|"
    r"rape|slut|whore|hooker|escort|prostitut\w*|"
    r"date me|sleep with (me|u|you)|hook ?up|make love|have sex|one night)\b",
    re.IGNORECASE,
)

# Keywords signalling a property search (used for lead_capture escape hatch)
_SEARCH_RE = re.compile(
    r"\b(show|properties|flats?|houses?|apartments?|bhk|budget|area|lakh|crore|crores|"
    r"available|search|find|looking|options|listings?|rooms?|bedroom|furnish)\b",
    re.IGNORECASE,
)

# SHOW the saved list ("show my favourites", "my saved properties", "what have I saved").
# Requires a show/my context so it doesn't collide with the SAVE verb below.
_SHORTLIST_RE = re.compile(
    r"\b(show|see|view|list|what'?s?|what\s+are|check|open|display)\b.{0,18}"
    r"\b(saved|favou?rites?|shortlist|liked|bookmark\w*)\b"
    r"|\bmy\s+(saved|favou?rites?|shortlist|liked|properties|list)\b"
    r"|\bsaved\s+(propert\w+|ones?|list)\b",
    re.IGNORECASE,
)

# SAVE/favourite a shown property ("save the 2nd one", "add this to favourites", "bookmark it").
_SAVE_RE = re.compile(
    r"\b(save|bookmark|favou?rite|shortlist|keep)\b(?!\s+(propert|list|ones?))"
    r"|add\b.{0,20}\b(favou?rites?|shortlist|saved)\b",
    re.IGNORECASE,
)

# EMI / home-loan questions
_EMI_RE = re.compile(
    r"\b(emi|e\.m\.i|home ?loan|loan|finance|financing|mortgage|down ?payment|"
    r"monthly (payment|installment|instalment|cost)|per month|installments?|instalments?|"
    r"interest rate|how much .{0,15}(per month|monthly))\b",
    re.IGNORECASE,
)

# Strong action words — buyer wants to act NOW (visit/book), not just discuss
_ACTION_RE = re.compile(
    r"\b(visit|book|schedule|arrange|contact|call me|call ?back|callback|broker|proceed|"
    r"i'?ll take|interested in (buying|booking)|site visit|see it in person|"
    r"meet|appointment|finalize|go ahead)\b",
    re.IGNORECASE,
)

# Clear intent to VISIT/see a property in person — should start booking, not comparison.
# Catches roundabout phrasings ("can I go see it", "I'd like to view this one").
_VISIT_INTENT_RE = re.compile(
    r"\b(go\s*see|see\s*(it|this|the\s*(place|flat|property|house))|view\s*(it|this|the)|"
    r"visit\s*(it|this|the)?|come\s*(see|view|over)|check\s*it\s*out|look\s*at\s*it|"
    r"can\s*i\s*(see|view|visit|come)|i'?d\s*like\s*to\s*(see|visit|view)|"
    r"want\s*to\s*(see|visit|view)|schedule\s*a?\s*visit|book\s*a?\s*visit|site\s*visit|"
    r"see\s*it\s*in\s*person|show\s*me\s*around)\b",
    re.IGNORECASE,
)

# "compare 1 and 2" / "which is better/cheaper" / "tell me about property 3" / "difference" /
# "how far is it from X" / "what's the distance"
_COMPARE_RE = re.compile(
    r"\b(compare|difference between|vs\.?|versus|"
    # superlatives/comparatives, allowing "which is THE biggest", "what's the cheapest"
    r"(which|what'?s?|which one'?s?)\s*(is|one is|one|are|the)?\s*(the\s+)?"
    r"(better|best|good|cheaper|cheapest|cheap|biggest|bigger|largest|larger|"
    r"smallest|smaller|most expensive|expensive|nicest|nicer|closest|closer|newest|newer)|"
    r"how far|how close|what'?s? the distance|distance (from|to)|approx\w* dist\w*|"
    r"dist\w* from|km from|far is it|far from|how many km|"
    r"tell me (more )?about (property |option |number |the )?(\d|first|second|third|last)|"
    r"more (details?|info) (on|about) (property |option |number |the )?(\d|first|second|third|last)|"
    r"property \d|option \d|(first|second|third|last) one)\b",
    re.IGNORECASE,
)

# Does the message carry any actual time/date signal? Used to avoid booking a non-time
# message (e.g. "can I reschedule") as if it were a visit slot.
_TIME_SIGNAL_RE = re.compile(
    r"\b(mon(day)?|tue(s|sday)?|wed(nesday)?|thu(r|rs|rsday)?|fri(day)?|sat(urday)?|sun(day)?|"
    r"today|tomorrow|tonight|morning|afternoon|evening|noon|midnight|weekend|"
    r"\d{1,2}\s*(am|pm)|\d{1,2}:\d{2}|\d{1,2}\s*o'?clock|at\s+\d{1,2}|"
    r"next\s+(week|mon|tue|wed|thu|fri|sat|sun)|this\s+(mon|tue|wed|thu|fri|sat|sun|weekend)|"
    r"\d{1,2}\s*(st|nd|rd|th)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|\d{1,2}/\d{1,2})\b",
    re.IGNORECASE,
)

# Buyer asking to see/choose visit time slots ("give me slots", "what timings", "available times").
_SHOW_SLOTS_RE = re.compile(
    r"\b(slots?|time\s*slots?|visit\s*times?|available\s*times?|timings?|"
    r"what\s*times?|which\s*times?|show.{0,10}times?|pick.{0,10}time)\b",
    re.IGNORECASE,
)

# Follow-up QUESTIONS about an already-shown property (not a new search): price
# negotiation, possession, carpet area, floor, facing, availability, etc. These must be
# answered from the shown listings — NOT trigger a fresh search.
_PROPERTY_QA_RE = re.compile(
    r"\b(negotiab\w*|negotiate|discount|best price|final price|price\s*(drop|flexible|fixed|negotiable)|"
    r"come down|any\s*(lower|less|cheaper price)|bargain|"
    r"carpet area|super area|built\s*up area|"
    r"ready to move|ready[-\s]?possession|possession|when can i move|move[-\s]?in|"
    r"registry|registration|brokerage|maintenance charge\w*|society charge\w*|deposit|"
    r"how old|age of (the|this|it)|year (built|of construction)|newly built|new construction|"
    r"which floor|what floor (is|are)|facing direction|which facing|vaastu|vastu|"
    r"is it (still )?available|still available|already (sold|booked|taken))\b",
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
    r"\b(remove.{0,10}budget|forget.{0,10}(the\s+)?budget|skip.{0,10}budget|drop.{0,10}budget|"
    r"leave.{0,10}budget|no budget|any budget|any price|ignore budget|"
    r"without.{0,6}(a\s+)?budget|don'?t have a budget|no fixed budget|"
    r"price.{0,10}(doesn'?t matter|not important|don'?t care|no bar)|"
    r"budget.{0,12}(doesn'?t matter|not important|remove|ignore|flexible|open|not fixed|"
    r"isn'?t fixed|no bar|no issue|whatever|no limit)|"
    r"(flexible|open).{0,6}(on\s+|with\s+)?budget|whatever.{0,6}budget|any amount|no limit|"
    r"not.{0,10}(worried|fussed|bothered).{0,12}(about\s+)?(the\s+)?(budget|price)|"
    r"just show me anything|show me anything)\b",
    re.IGNORECASE,
)

# A short, VAGUE "no preference" reply — interpret it against whatever we just asked.
# Matches: "any", "anything", "nay would work" (typo), "whatever", "no preference",
# "doesn't matter", "you decide", "up to you". Anchored so it won't catch real searches.
_VAGUE_ANY_RE = re.compile(
    r"^\s*"
    r"(any|anything|anyone|nay|ny|whatever|all|either|none|"
    r"no\s*(preference|idea)|doesn'?t\s*matter|don'?t\s*care|not\s*sure|not\s*fussed|"
    r"you\s*(decide|choose|pick|tell\s*me)|surprise\s*me|up\s*to\s*you)"
    r"(\s+(one|thing|type|kind|bhk|budget|price|area|place))?"
    r"(\s+(would|will|is|are|can|should))?"
    r"(\s+(work|works|fine|okay|ok|good|do))?"
    r"\s*[.!,]*\s*$",
    re.IGNORECASE,
)

# "any BHK" / "doesn't matter" — clear BHK + property_type filters
# ONLY explicit BHK/type-clearing phrases. Generic vague replies ("any is fine",
# "doesn't matter", "you decide") are handled context-aware by _VAGUE_ANY_RE so they
# clear the slot actually being asked about — not an already-specified BHK.
_ANY_BHK_RE = re.compile(
    r"\b(any bhk|any type|any flat|any house|any property|any configuration|any size|"
    r"whatever type|not.{0,8}(specific|particular).{0,8}bhk|bhk.{0,10}doesn'?t matter|"
    r"don'?t care.{0,10}bhk|not.{0,5}(2|3|4) bhk)\b",
    re.IGNORECASE,
)


# A pure greeting with no request in it ("hi", "hello", "namaste", "good morning").
_GREETING_ONLY_RE = re.compile(
    r"^\s*(hi+|hey+|h[ae]llo+|helo+|hii+|namaste+|namaskar|hola|yo|"
    r"good\s*(morning|afternoon|evening|day)|start|begin)"
    r"[\s,!.]*\b(riya|there|ji)?[\s,!.]*$",
    re.IGNORECASE,
)

# A bare affirmation ("yes", "ok", "sure", "sounds good") with nothing else — after we've
# shown properties this means "I'm interested", NOT "search again". Anchored so it never
# catches a real query like "yes show me 3 BHK in Aliganj".
_AFFIRM_ONLY_RE = re.compile(
    r"^\s*(y+e+s+|y+e+a+h*|y+u+p+|yep|ya+|ha+n*|ji+|ok+a*y*|okie+|sure+|"
    r"sounds?\s*good|great|perfect|nice|cool|good|fine|alright|right|"
    r"definitely|absolutely|please|pls|👍+|🙂+|👌+)"
    r"[\s,!.]*$",
    re.IGNORECASE,
)


_AMENITY_SYNONYM_GROUPS = [
    ["lift", "elevator"],
    ["gym", "gymnasium", "fitness"],
    ["pool", "swimming"],
    ["parking", "car park", "reserved parking"],
    ["power backup", "generator", "inverter"],
    ["security", "guard", "cctv", "gated"],
    ["garden", "park facing", "lawn"],
]


def _amenity_synonyms(amenity: str) -> list[str]:
    """Return the synonym set for a requested amenity (singular/plural tolerant)."""
    a = amenity.lower().strip().rstrip("s")  # 'lifts' → 'lift'
    for group in _AMENITY_SYNONYM_GROUPS:
        if any(a == g.rstrip("s") or a in g for g in group):
            return group
    return [a, amenity.lower().strip()]


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


_ORDINAL_MAP = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "last": -1,
}
_PROP_NUM_RE = re.compile(r"\b(?:property|option|number|no\.?|#|card|the)\s*(\d)\b", re.IGNORECASE)
_ORDINAL_RE = re.compile(r"\b(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|last)\b", re.IGNORECASE)


def _resolve_referenced_property_id(user_message: str, conv: ConversationManager) -> str | None:
    """
    Figure out which already-shown property the user is referring to
    ("the first one", "property 2", "book #3", "the last one"). Returns its id or None.
    """
    cards = conv.requirements.get("_last_shown_cards") or []
    if not cards:
        return None
    idx = None
    m = _PROP_NUM_RE.search(user_message)
    if m:
        idx = int(m.group(1))
    else:
        m2 = _ORDINAL_RE.search(user_message)
        if m2:
            idx = _ORDINAL_MAP.get(m2.group(1).lower())
    if idx == -1:
        return cards[-1].get("id")
    if idx and 1 <= idx <= len(cards):
        return cards[idx - 1].get("id")
    return None


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


def _llm(messages: list[dict], temperature: float = 0.7, max_tokens: int = 700) -> str:
    # Gemini-primary, Groq-fallback (see agent/llm_client.py).
    from agent.llm_client import complete
    return complete(messages, temperature=temperature, max_tokens=max_tokens)


def _sys(user_name: str | None = None) -> str:
    if user_name:
        return SYSTEM_PROMPT_NAMED.format(name=user_name)
    return SYSTEM_PROMPT


def _get_user_name(conv: ConversationManager) -> str | None:
    return (conv.requirements.get("_profile") or {}).get("name")


def _has_any_requirement(req: dict) -> bool:
    """True if the buyer has given any real search criterion yet."""
    return any(req.get(k) for k in
               ("area", "bhk", "property_type", "max_budget_cr", "min_budget_cr",
                "named_landmark", "nearby"))


# Forward-moving replies for a bare "yes" after results — rotated so it never repeats.
_AFFIRM_REPLIES = [
    "Lovely! Which of these caught your eye{nm}? Tell me the one you like and I can arrange a visit whenever you're ready. 🙂",
    "Great{nm}! Would you like me to set up a site visit for any of these — just tell me which one feels right?",
    "Perfect{nm}! Want me to share more details on one of these, or shall I arrange a visit so you can see it in person?",
]


def _handle_affirmation(conv: ConversationManager, user_name: str | None) -> str:
    """Buyer said a bare 'yes/ok' after seeing properties — nudge forward, don't re-search."""
    nm = f", {user_name}" if user_name else ""
    idx = conv.get_recommendation_count() % len(_AFFIRM_REPLIES)
    return _AFFIRM_REPLIES[idx].format(nm=nm)


def _handle_show_slots(conv: ConversationManager, user_name: str | None) -> str:
    """Buyer asked to see visit time options — show the slot menu (book or change)."""
    req = conv.requirements
    profile = req.get("_profile") or {}
    nm = f", {user_name}" if user_name else ""

    # Already booked → offer slots to switch to (reschedule).
    if req.get("_last_meeting_id"):
        req["_pending_meeting"] = {
            "meeting_id": req["_last_meeting_id"],
            "lead_id": req.get("_last_lead_id"),
            "property_id": req.get("_liked_property_id"),
            "phone": profile.get("phone", ""),
            "name": profile.get("name") or user_name,
            "reschedule": True,
        }
        conv.set_stage("scheduling")
        slots = _suggest_visit_slots(); req["_suggested_slots"] = slots
        menu = "\n".join(f"{i+1}️⃣  {s['label']}" for i, s in enumerate(slots))
        return (f"Sure{nm}! Here are some times you could switch to:\n\n{menu}\n\n"
                f"Reply *1*, *2* or *3*, or tell me any other day & time.")

    # Not booked yet, but we know who they are + a property in focus → start booking.
    if req.get("_liked_property_id") and profile.get("name") and profile.get("phone"):
        return _begin_scheduling(conv, profile["name"], profile["phone"])

    # No property chosen yet.
    return (f"Happy to set up a visit{nm}! Which of the properties would you like to see — "
            f"tell me the one and I'll pull up some times.")


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

    # Property deeplink from browse page: "__init_prop__:PROP_ID:2 BHK in Gomti Nagar at ₹45 L"
    if user_message.startswith("__init_prop__:"):
        parts = user_message.split(":", 2)
        prop_id = parts[1] if len(parts) > 1 else ""
        prop_desc = parts[2] if len(parts) > 2 else "a property"
        name = profile.get("name", "")
        greeting = f"Hi {name}! " if name else "Hi! "
        # Store property context so follow-up messages about this property work
        conv.requirements["_deeplink_property_id"] = prop_id
        reply = (
            f"{greeting}I see you're interested in **{prop_desc}**. "
            f"I can tell you more about it, arrange a site visit, or help you compare it with similar options. "
            f"What would you like to do?"
        )
        return {"reply": reply, "properties": []}

    # Auto-init: page load / reload — handle here so __init__ never hits intent extractor
    if user_message == "__init__":
        if step == "complete":
            name = profile.get("name")
            # Reset all search requirements on each session start so the bot asks
            # qualifying questions fresh instead of using stale session data.
            # Keep only _profile (name, phone) — discard old budget/area/BHK/flags.
            # Reset the search on reload, but KEEP the buyer's profile AND their saved
            # shortlist (otherwise "Saved 2" in the UI but the bot thinks none are saved).
            saved = conv.requirements.get("_shortlist") or []
            conv.requirements = {"_profile": profile}
            if saved:
                conv.requirements["_shortlist"] = saved
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

def process_message(session_id: str, user_message: str, platform: str = "web",
                    display_name: str | None = None, user_phone: str | None = None) -> dict:
    """
    Process one user message.
    Returns {"reply": str, "properties": list[dict]}.

    Web onboarding messages (name/phone) are intentionally NOT added to chat history
    so they don't confuse the LLM intent extractor (e.g. a phone number in history
    should not trigger lead_intent = "strong").

    display_name: platform-provided name (e.g. WhatsApp profile name) — used to greet
    the buyer warmly without an onboarding step on messaging channels.
    """
    conv = ConversationManager(session_id, platform)
    conv.load()

    # On messaging platforms we don't run the web name/phone onboarding, but we DO get
    # the sender's name for free from the platform — store it so Riya can use it.
    if display_name:
        profile = conv.requirements.get("_profile") or {}
        if not profile.get("name"):
            clean = display_name.strip()
            if 1 < len(clean) <= 40 and clean.lower() not in _NOT_A_NAME:
                profile["name"] = clean
                conv.requirements["_profile"] = profile

    # On WhatsApp the sender's number IS their contact number — capture it so we never
    # ask "what's your number?" (we already have it). Stored as the 10-digit local part.
    if user_phone:
        profile = conv.requirements.get("_profile") or {}
        if not profile.get("phone"):
            digits = re.sub(r"\D", "", user_phone)
            if len(digits) >= 10:
                profile["phone"] = digits[-10:]
                conv.requirements["_profile"] = profile

    # ── Smart reschedule: if the broker asked "are you free at X?", interpret the
    # customer's reply here and auto-reschedule when they confirm. ────────────
    if (user_message not in ("__init__",) and not user_message.startswith("__init_prop__:")):
        _ask = conv.requirements.get("_pending_reschedule_ask")
        if _ask:
            try:
                from agent.broker_confirmation import handle_customer_reschedule_reply
                _r = handle_customer_reschedule_reply(conv, user_message, _ask)
                if _r is not None:
                    conv.save()
                    return {"reply": _r, "properties": []}
            except Exception as _e:
                logger.debug(f"smart-reschedule reply skipped: {_e}")

    # ── Two-way loop: if the broker recently messaged this customer, forward the
    # customer's reply back to the broker so nothing slips through. ───────────
    if (user_message not in ("__init__",) and not user_message.startswith("__init_prop__:")):
        _relay_broker = conv.requirements.get("_relay_broker")
        if _relay_broker:
            try:
                from agent.broker_confirmation import forward_customer_reply_to_broker
                forward_customer_reply_to_broker(_relay_broker, session_id,
                                                 conv.requirements, user_message)
            except Exception as _e:
                logger.debug(f"relay-to-broker skipped: {_e}")

    # ── Guardrail (all platforms/stages): inappropriate or abusive input ─────
    # Runs before onboarding AND routing so it can't be smuggled in as a "name"
    # or slip through the clarify flow. Decline politely, do not store, do not engage.
    if (user_message != "__init__" and not user_message.startswith("__init_prop__:")
            and _INAPPROPRIATE_RE.search(user_message)):
        user_name = _get_user_name(conv)
        name = f" {user_name}" if user_name else ""
        return {
            "reply": (
                f"I'm Riya, your property consultant for Lucknow{name} — I'm here only to help "
                "you find a home, so let's keep it professional. 🙂 What kind of property are you "
                "looking for, and what's your budget?"
            ),
            "properties": [],
        }

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
    stripped = user_message.strip()

    # (Inappropriate/abusive input is guarded at the top of process_message.)

    # ── Pure greeting with no request ("hi", "namaste") at the start of a chat ──
    # Give a warm, human first impression instead of a cold "what's your budget?".
    if (_GREETING_ONLY_RE.match(stripped)
            and conv.stage == "discovery"
            and not _has_any_requirement(conv.requirements)):
        nm = f" {user_name}" if user_name else ""
        return (
            f"Namaste{nm}! 🙏 I'm Riya, your property consultant here in Lucknow. "
            f"I'd love to help you find the right home. To get started, could you tell me "
            f"the area you have in mind, your budget, and how many bedrooms you'd like? "
            f"Even one of those is a great start. 🏡"
        ), []

    # ── Bare "yes / ok / sure" right after we showed properties ────────────────
    # Means "I'm interested" — move the conversation forward; do NOT re-run the search
    # (that was re-showing the same listings every turn).
    if (conv.stage == "recommending"
            and conv.requirements.get("_last_shown_cards")
            and _AFFIRM_ONLY_RE.match(stripped)
            and not _ACTION_RE.search(user_message)
            and not _MORE_OPTIONS_RE.search(user_message)):
        return _handle_affirmation(conv, user_name), conv.requirements.get("_last_shown_cards", [])

    # ── "Give me slots / what timings" — show visit times, don't run a search ──
    if (_SHOW_SLOTS_RE.search(user_message)
            and (conv.requirements.get("_last_meeting_id") or conv.requirements.get("_liked_property_id"))
            and not conv.is_lead_capture_stage()
            and conv.stage != "scheduling"):
        return _handle_show_slots(conv, user_name), conv.requirements.get("_last_shown_cards", [])

    # ── Show saved/favourite properties ──────────────────────────────────────
    if _SHORTLIST_RE.search(user_message):
        return _show_shortlist(conv, user_name)

    # ── Save/favourite a shown property ───────────────────────────────────────
    if (_SAVE_RE.search(user_message)
            and not conv.is_lead_capture_stage() and conv.stage != "scheduling"):
        return _handle_save_favourite(conv, user_message, user_name), conv.requirements.get("_last_shown_cards", [])

    # ── EMI / loan question ──────────────────────────────────────────────────
    if _EMI_RE.search(user_message) and not conv.is_lead_capture_stage():
        return _handle_emi(conv, user_name), conv.requirements.get("_last_shown_cards", [])

    # ── Compare / detail request about already-shown properties ───────────────
    # Only if we've shown properties AND the user isn't trying to act (visit/book) or
    # in lead capture. A message like "I love the first one, can I visit?" must fall
    # through to intent extraction → strong intent → lead capture, NOT comparison.
    if (conv.requirements.get("_last_shown_text")
            and (_COMPARE_RE.search(user_message) or _PROPERTY_QA_RE.search(user_message))
            and not _ACTION_RE.search(user_message)
            and not _VISIT_INTENT_RE.search(user_message)   # "the 2nd one, can I see it?" → book, not compare
            and not conv.is_lead_capture_stage()):
        return _compare_properties(conv, user_message, user_name), conv.requirements.get("_last_shown_cards", [])

    # ── Stage: collecting name + phone ───────────────────────────────────────
    if conv.is_lead_capture_stage():
        if _SEARCH_RE.search(user_message):
            conv.set_stage("discovery")  # escape hatch
        else:
            return _handle_lead_capture(conv, user_message, user_name), []

    # ── Stage: collecting a preferred visit time ─────────────────────────────
    if conv.stage == "scheduling":
        return _handle_scheduling(conv, user_message, user_name), []

    # ── Buyer sends an email after booking → confirm + calendar invite by mail ──
    if conv.requirements.get("_last_meeting_id") and _extract_email(user_message) \
            and not conv.requirements.get("_liked_property_id_changed"):
        email = _extract_email(user_message)
        profile = conv.requirements.get("_profile") or {}
        profile["email"] = email
        conv.requirements["_profile"] = profile
        name = profile.get("name") or user_name or "there"
        _dt = None
        if conv.requirements.get("_last_visit_dt"):
            try:
                from datetime import datetime as _datetime
                _dt = _datetime.fromisoformat(conv.requirements["_last_visit_dt"])
            except Exception:
                _dt = None
        _send_visit_confirmation_email(email, name, conv.requirements.get("_last_visit_when", "your visit"),
                                       conv.requirements.get("area") or "Lucknow",
                                       conv.requirements.get("_last_gcal"), _dt)
        return f"Done — I've emailed the visit details and a calendar invite to {email}. 📧", []

    # ── Reschedule an already-booked visit ("can we change the time?") ───────
    if _RESCHEDULE_RE.search(user_message) and conv.requirements.get("_last_meeting_id"):
        profile = conv.requirements.get("_profile") or {}
        conv.requirements["_pending_meeting"] = {
            "meeting_id": conv.requirements["_last_meeting_id"],
            "lead_id": conv.requirements.get("_last_lead_id"),
            "property_id": conv.requirements.get("_liked_property_id"),
            "phone": profile.get("phone", ""),
            "name": profile.get("name") or user_name,
            "reschedule": True,
        }
        conv.set_stage("scheduling")
        nm = (profile.get("name") or user_name or "")
        # If they already gave the new time in the same message ("change it to Monday 6 pm"),
        # parse it now instead of asking again.
        if _parse_visit_time(user_message):
            return _handle_scheduling(conv, user_message, user_name), []
        return (f"Sure{', ' + nm if nm else ''} — what new day and time would suit you better? "
                "(e.g. \"Sunday afternoon\" or \"Monday 6 pm\")"), []

    # ── After completed lead, allow new search (post_lead cooldown) ──────────
    if conv.stage in ("done", "post_lead"):
        remaining = conv.requirements.get("_post_lead_turns", 0)
        if remaining > 0:
            conv.requirements["_post_lead_turns"] = remaining - 1
        else:
            conv.set_stage("discovery")

    # ── Guard: ignore very short/noise messages mid-conversation ─────────────
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

    # Context-aware vague reply ("any", "nay would work", "whatever", "no preference"):
    # the user is answering whatever we just asked, so clear THAT pending slot instead
    # of looping the same question. Pending slot = first still-missing of budget→area→config.
    if _VAGUE_ANY_RE.match(stripped):
        r = conv.requirements
        has_budget = bool(r.get("max_budget_cr") or r.get("min_budget_cr")) or r.get("_budget_cleared")
        has_location = bool(r.get("area") or r.get("named_landmark") or r.get("nearby")) or r.get("_area_cleared")
        has_config = bool(r.get("bhk") or r.get("property_type")) or r.get("_bhk_cleared")
        if not has_budget:
            clear_budget = True
        elif not has_config:
            clear_bhk = True
        elif not has_location:
            is_diff_area = True

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

    # Code-level visit intent: a clear "let me see/visit it" — even phrased roundabout — is
    # a booking signal, even if the LLM under-tagged it. Only once we've shown properties.
    if conv.requirements.get("_last_shown_cards") and _VISIT_INTENT_RE.search(user_message):
        lead_level = "strong"

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

    # ── Strong intent -> book ────────────────────────────────────────────────
    if lead_level == "strong":
        # Capture WHICH property they want so the broker gets the actual listing.
        ref = _resolve_referenced_property_id(user_message, conv)
        if ref:
            conv.requirements["_liked_property_id"] = ref
        elif not conv.requirements.get("_liked_property_id"):
            cards = conv.requirements.get("_last_shown_cards") or []
            if cards:
                conv.requirements["_liked_property_id"] = cards[0].get("id", "")
        # If we already have their name + phone, DON'T ask again — go straight to
        # picking a visit time.
        profile = conv.requirements.get("_profile") or {}
        if profile.get("name") and profile.get("phone"):
            return _begin_scheduling(conv, profile["name"], profile["phone"]), []
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
        landmark = req.get("named_landmark")
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
        else:
            # Landmark found — but is anything actually CLOSE to it? Distances are
            # area-level, so if the nearest is far, don't pretend it's "near".
            dists = [p.get("named_landmark_distance_km") for p in properties
                     if p.get("named_landmark_distance_km") is not None]
            nearest = min(dists) if dists else None
            if nearest is not None and nearest > _LANDMARK_NEAR_KM:
                near_area = next(
                    ((p.get("data") or {}).get("location", {}).get("area_name") for p in properties
                     if (p.get("data") or {}).get("location", {}).get("area_name")),
                    "nearby",
                )
                filter_note = (
                    f"(IMPORTANT: I have NOTHING listed right near {landmark} — the closest "
                    f"properties are about {nearest:.1f} km away in {near_area}. Be honest and warm: "
                    f"tell the buyer you don't have anything close to {landmark} right now, the nearest "
                    f"are ~{nearest:.0f} km away, and offer to either show those or have a consultant "
                    f"source something closer. Do NOT call {nearest:.0f} km 'near' or 'close'.)"
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
    if req_amenities and properties:
        # Bring properties that actually HAVE the requested amenity to the front, so
        # "show me homes with a lift" surfaces real lift properties first.
        def _has_amenity(p: dict, syns: list) -> bool:
            blob = " ".join(p.get("top_amenities") or []).lower()
            return any(s in blob for s in syns)

        all_syns = [_amenity_synonyms(a) for a in req_amenities]
        def _amenity_score(p):
            return sum(1 for syns in all_syns if _has_amenity(p, syns))
        properties.sort(key=_amenity_score, reverse=True)

        # Honesty note: if NONE of the shown properties have a requested amenity, say so.
        # This ALWAYS runs (even when another note exists) and takes priority, because
        # claiming a feature a property doesn't have is the most damaging error.
        missing = [
            req_am for req_am, syns in zip(req_amenities, all_syns)
            if not any(_has_amenity(p, syns) for p in properties)
        ]
        if missing:
            amen_note = (
                f"(IMPORTANT: none of these properties list {', '.join(repr(m) for m in missing)} "
                f"among their amenities. Tell the buyer honestly you don't have a match with "
                f"{', '.join(missing)} right now, then describe the closest options. "
                f"You MUST NOT claim any property has a {missing[0]}.)"
            )
            filter_note = (amen_note + " " + filter_note) if filter_note else amen_note

    # ── "More options" exhausted — suggest alternatives ───────────────────────
    if not properties and is_more_request:
        return _suggest_alternatives(conv, user_name), []

    if properties:
        new_ids = [p.get("id") for p in properties if p.get("id")]
        conv.requirements["_shown_ids"] = list(dict.fromkeys(shown_ids + new_ids))

        cards = [to_card(p) for p in properties]
        # Remember the most-recent visible set so the user can ask
        # "which is better, 1 or 2?" / "tell me about property 3" / "how far is it?" after.
        conv.requirements["_last_shown_text"] = format_properties_for_llm(properties)
        conv.requirements["_last_shown_cards"] = cards
        props_text = conv.requirements["_last_shown_text"]

        # A named landmark is a per-search anchor. Now that its distances are baked into
        # the cards + summary, clear it so the NEXT search doesn't silently keep filtering
        # "near <old place>" (e.g. user pivots to "homes under 20 lakh with a lift").
        # Follow-up "how far is it?" questions read the distance from the stored cards.
        conv.requirements["named_landmark"] = None
        conv.requirements["named_landmark_max_km"] = None
        # Don't repeat the SAME availability caveat turn after turn ("no gym…", "no gym…").
        # Show it once; suppress it on the next turn if it's identical.
        if filter_note and filter_note == conv.requirements.get("_last_filter_note"):
            filter_note = ""
        conv.requirements["_last_filter_note"] = filter_note
        avail_note = f"\n⚠️ availability_note: {filter_note}\n" if filter_note else ""

        # Soft-interest nudge is FOLDED into this same prompt — no second LLM call.
        # (Previously the nudge was a separate generate pass; cutting it saves ~1 LLM
        # call per recommend turn, which matters on free-tier rate limits.) Skip when
        # there's an honesty note so we don't dilute the "we don't have X" message.
        rec_count = conv.get_recommendation_count()
        nudge = (not filter_note) and (
            (lead_level == "soft") or (rec_count >= _AUTO_NUDGE_AFTER and lead_level == "none")
        )
        nudge_hint = (
            "\n\nThe buyer is engaging — for your closing line, gently ask which of these stood "
            "out to them or whether they'd like to see more options (instead of a generic invite)."
            if nudge else ""
        )
        user_prompt = PROPERTY_RECOMMENDATION_PROMPT.format(
            count=len(properties),
            requirements=_requirements_summary(req),
            properties_text=props_text,
            availability_note=avail_note,
        ) + nudge_hint
    else:
        cards = []
        user_prompt = NO_RESULTS_PROMPT.format(requirements=_requirements_summary(req))

    messages = [{"role": "system", "content": _sys(user_name)}]
    messages += conv.get_history_for_llm()
    messages.append({"role": "user", "content": user_prompt})
    reply = _llm(messages, max_tokens=320)

    # Track the top result as the default "interested" property every time we show
    # results, so a later "book a visit" always has a property to attach to the lead.
    # A specific pick (soft interest, or "tell me about #3") overrides this elsewhere.
    if properties:
        conv.requirements["_liked_property_id"] = properties[0].get("id", "")
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
    from agent.lead_collector import is_fake_phone
    profile = conv.requirements.get("_profile") or {}
    name, phone = extract_name_and_phone(user_message)
    email = _extract_email(user_message)
    # Fall back to anything we already know about this buyer.
    name = name or profile.get("name")
    phone = phone or profile.get("phone")
    if email:
        profile["email"] = email
        conv.requirements["_profile"] = profile

    # Reject obviously bogus numbers (9999999999, 1234567890) before saving a junk lead.
    if phone and is_fake_phone(phone):
        return ("That number doesn't look quite right — could you double-check and share "
                "your 10-digit mobile number so our consultant can reach you?")

    if name and phone:
        return _begin_scheduling(conv, name, phone)

    if phone:
        return "Got your number! And what name should I note it under?"

    messages = [{"role": "system", "content": _sys(user_name)}]
    messages += conv.get_history_for_llm()
    messages.append({
        "role": "user",
        "content": (
            "The buyer wants to proceed but hasn't shared their name and phone yet. "
            "Gently ask for their name and 10-digit mobile in one warm sentence."
        ),
    })
    return _llm(messages, temperature=0.5, max_tokens=120)


def _begin_scheduling(conv: ConversationManager, name: str, phone: str) -> str:
    """Save the lead (using whatever we know) and move into collecting a visit time.
    Shared by the lead-capture handler and the 'already have your details' fast path."""
    liked_id = conv.requirements.get("_liked_property_id")
    lead = create_lead(
        session_id=conv.session_id, requirements=conv.requirements,
        name=name, phone=phone, property_id=liked_id,
    )
    profile = conv.requirements.get("_profile") or {}
    profile["name"] = name
    profile["phone"] = phone
    conv.requirements["_profile"] = profile
    conv.requirements["_pending_meeting"] = {
        "lead_id": lead.get("id") if lead else None,
        "property_id": liked_id, "phone": phone, "name": name,
    }
    conv.set_stage("scheduling")

    # Offer concrete slots the buyer can simply pick (or change to any other time).
    slots = _suggest_visit_slots()
    conv.requirements["_suggested_slots"] = slots
    menu = "\n".join(f"{i+1}️⃣  {s['label']}" for i, s in enumerate(slots))
    return (
        f"Thank you, {name}! 🎉 I've shared your details with our consultant. "
        f"When would you like to visit? A few easy options:\n\n{menu}\n\n"
        f"Just reply *1*, *2* or *3* — or tell me any other day & time that suits you "
        f"(like \"tomorrow evening\"). You can change it anytime. 🗓️"
    )


_SKIP_VISIT_RE = re.compile(r"^\s*(skip|later|not now|no thanks?|nah|call me|you call|whenever|"
                            r"any ?time|flexible|don'?t know|not sure)\b", re.IGNORECASE)

# "change the time", "reschedule", "different slot" — adjust an already-booked visit.
_RESCHEDULE_RE = re.compile(
    r"\b(reschedul\w*|change .{0,14}(time|slot|date|day|booking|appointment|visit)|"
    r"different (time|day|date|slot)|another (time|day|slot|date)|move .{0,14}(visit|appointment|booking|slot)|"
    r"new (time|slot|date)|can we change|push .{0,10}(time|visit))\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _extract_email(text: str) -> str | None:
    m = _EMAIL_RE.search(text or "")
    return m.group(0) if m else None


def _build_ics(dt, title: str, description: str, location: str, attendee_email: str | None = None) -> str:
    """A minimal valid .ics invite (floating local time) with a 1-hour reminder."""
    from datetime import timedelta
    import uuid as _uuid
    start = dt.strftime("%Y%m%dT%H%M%S")
    end = (dt + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
    stamp = dt.strftime("%Y%m%dT%H%M%S")
    uid = f"{_uuid.uuid4().hex}@riya-realestate"
    att = f"ATTENDEE;CN={attendee_email};RSVP=TRUE:mailto:{attendee_email}\r\n" if attendee_email else ""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Riya Real Estate//EN\r\nMETHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:{stamp}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
        f"SUMMARY:{title}\r\nDESCRIPTION:{description}\r\nLOCATION:{location}\r\n"
        f"{att}STATUS:CONFIRMED\r\n"
        "BEGIN:VALARM\r\nTRIGGER:-PT1H\r\nACTION:DISPLAY\r\nDESCRIPTION:Property visit reminder\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def _gcal_link(dt, title: str, details: str = "", location: str = "Lucknow") -> str | None:
    """A free 'Add to Google Calendar' link (no API/OAuth) the buyer can tap to add the
    visit to THEIR own calendar."""
    if not dt:
        return None
    from datetime import timedelta
    import urllib.parse
    start = dt.strftime("%Y%m%dT%H%M%S")
    end = (dt + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
    q = urllib.parse.urlencode({
        "action": "TEMPLATE", "text": title, "dates": f"{start}/{end}",
        "details": details, "location": location,
    })
    return f"https://calendar.google.com/calendar/render?{q}"


def _handle_scheduling(conv: ConversationManager, user_message: str, user_name: str | None) -> str:
    """Collect/adjust the buyer's preferred visit time and create or update the meeting."""
    from database.supabase_client import save_meeting, update_meeting
    pending = conv.requirements.get("_pending_meeting") or {}
    profile = conv.requirements.get("_profile") or {}
    name = pending.get("name") or user_name or "there"
    phone = pending.get("phone", "")

    # If they slip in an email here, capture it (so we can email a confirmation).
    em = _extract_email(user_message)
    if em:
        profile["email"] = em
        conv.requirements["_profile"] = profile

    def _finish():
        conv.set_stage("post_lead")
        conv.requirements["_post_lead_turns"] = 3
        conv.requirements.pop("_pending_meeting", None)
        conv.requirements.pop("_suggested_slots", None)

    if _SKIP_VISIT_RE.match(user_message.strip()):
        _finish()
        return VISIT_SKIP_TEMPLATE.format(name=name, phone=phone)

    # Buyer picked one of the suggested slots by number ("2") or ordinal ("second").
    dt, when = None, None
    slots = conv.requirements.get("_suggested_slots") or []
    msg_s = user_message.strip()
    m_num = re.match(r"^\s*(?:option\s*|slot\s*|number\s*)?([123])\b\s*[.!]?\s*$", msg_s, re.IGNORECASE)
    m_ord = re.match(r"^\s*(first|second|third|1st|2nd|3rd)\b", msg_s, re.IGNORECASE)
    if slots and (m_num or m_ord):
        ord_map = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}
        idx = (int(m_num.group(1)) - 1) if m_num else ord_map[m_ord.group(1).lower()]
        if 0 <= idx < len(slots):
            from datetime import datetime as _dtmod
            try:
                dt = _dtmod.fromisoformat(slots[idx]["iso"])
                when = slots[idx]["label"]
            except Exception:
                dt, when = None, None

    if dt is None:
        dt, when = _parse_visit_time(user_message)

    # "give me slots / what timings" while scheduling → re-show the numbered menu.
    if dt is None and _SHOW_SLOTS_RE.search(user_message):
        slots = _suggest_visit_slots()
        conv.requirements["_suggested_slots"] = slots
        menu = "\n".join(f"{i+1}️⃣  {s['label']}" for i, s in enumerate(slots))
        return (f"Sure, {name}! Here are some options:\n\n{menu}\n\n"
                f"Reply *1*, *2* or *3*, or tell me any other day & time.")

    # Don't book a non-time message as a slot. If no real datetime AND no time signal at all
    # (e.g. "can I reschedule", "actually wait", a question), ask for the time instead.
    if dt is None and not _TIME_SIGNAL_RE.search(user_message):
        # A reschedule/change phrasing → acknowledge it; otherwise a generic prompt.
        if _RESCHEDULE_RE.search(user_message):
            return (f"Of course, {name}! What new day and time would you like? "
                    f"(e.g. \"Saturday 4 pm\" or \"tomorrow evening\")")
        return (f"Sure, {name}! What day and time works for you? Reply with a slot number, "
                f"or tell me something like \"Saturday 4 pm\" or \"tomorrow evening\".")

    # Real availability check against our own bookings — "is that slot free?"
    if dt:
        from database.supabase_client import meeting_slot_taken
        try:
            if meeting_slot_taken(dt.isoformat(), exclude_id=pending.get("meeting_id")):
                from datetime import timedelta
                alt = dt + timedelta(hours=1)
                return (f"Ah, {name}, *{when}* just got taken. The slot at "
                        f"*{_fmt_visit(alt)}* is open though — shall I lock that in, or would "
                        "another day suit you better?")
        except Exception as e:
            logger.warning(f"availability check failed (continuing): {e}")

        # Also check broker's real Google Calendar (if configured)
        try:
            from notifications.calendar_client import is_broker_free
            free = is_broker_free(dt)
            if free is False:  # explicitly busy (None = not configured, skip)
                from datetime import timedelta
                alt = dt + timedelta(hours=1)
                return (f"The consultant's calendar shows *{when}* is busy. "
                        f"How about *{_fmt_visit(alt)}*? I can check that too.")
        except Exception as _e:
            logger.debug(f"Google Calendar check skipped: {_e}")

    fields = {
        "property_id": pending.get("property_id"),
        "status": "pending",
        "notes": f"Buyer requested visit: {user_message.strip()[:200]}",
    }
    if dt:
        fields["scheduled_at"] = dt.isoformat()

    meeting_id = pending.get("meeting_id")
    try:
        if meeting_id:                      # reschedule → update the same meeting
            update_meeting(meeting_id, fields)
        else:
            fields["lead_id"] = pending.get("lead_id")
            saved = save_meeting(fields)
            meeting_id = (saved or {}).get("id")
    except Exception as e:
        logger.error(f"save/update meeting failed: {e}")

    if meeting_id:
        conv.requirements["_last_meeting_id"] = meeting_id
        conv.requirements["_last_lead_id"] = pending.get("lead_id")

    _finish()

    # ── Ping the broker via WhatsApp to confirm they're free ─────────────────
    # This is the two-way scheduling flow: broker replies YES → meeting is locked in
    # on both calendars; NO → buyer is asked to suggest another time.
    broker_asked = False
    try:
        from database.supabase_client import get_broker_for_area
        from agent.broker_confirmation import ask_broker_availability
        area_for_broker = conv.requirements.get("area") or "Lucknow"
        broker = get_broker_for_area(area_for_broker) or {}
        # Single-broker setup: the configured BROKER_WHATSAPP_PHONE is the source of truth
        # and takes priority over any (possibly placeholder) phone in the brokers table.
        broker_phone = (os.environ.get("BROKER_WHATSAPP_PHONE")
                        or os.environ.get("WHATSAPP_BROKER_PHONE")
                        or broker.get("phone"))
        if broker_phone and dt:
            broker_asked = ask_broker_availability(
                buyer_name=name,
                buyer_phone=phone,
                buyer_session_id=conv.session_id,
                proposed_when=when,
                proposed_dt=dt,
                property_id=pending.get("property_id"),
                lead_id=pending.get("lead_id"),
                meeting_id=meeting_id,
                broker_phone=broker_phone,
            )
    except Exception as e:
        logger.warning(f"ask_broker_availability failed (non-fatal): {e}")

    # Free "add to your calendar" link the buyer can tap (no calendar account needed on our side).
    area = conv.requirements.get("area") or "Lucknow"
    gcal = _gcal_link(dt, "Property visit with Riya", f"Visit arranged via Riya. {when}.", f"{area}, Lucknow") if dt else None
    conv.requirements["_last_visit_when"] = when
    conv.requirements["_last_gcal"] = gcal
    conv.requirements["_last_visit_dt"] = dt.isoformat() if dt else None
    cal_line = f"\n\n📅 [Add to your calendar]({gcal})" if gcal else ""

    # Email + SMS confirmation
    if profile.get("email"):
        _send_visit_confirmation_email(profile.get("email"), name, when, area, gcal, dt)
        cal_line += f"  ·  ✉️ invite sent to {profile['email']}"
    else:
        cal_line += "  ·  ✉️ share your email for a calendar invite"

    # SMS confirmation (fires if buyer phone known + FAST2SMS_API_KEY set)
    try:
        from notifications.sms_notifier import send_visit_sms_buyer
        if phone:
            send_visit_sms_buyer(phone, name, when, area)
    except Exception as _sms_err:
        logger.debug(f"SMS skipped: {_sms_err}")

    property_part = " for the property you liked" if pending.get("property_id") else ""
    if broker_asked:
        # AI is actively confirming with the broker — set the buyer's expectation accordingly.
        base = (f"Perfect, {name}! 🕐 I'm checking *{when}*{property_part} with our property "
                f"consultant right now — I'll confirm the moment they reply. "
                f"Need a different time? Just tell me.")
    else:
        base = VISIT_SCHEDULED_TEMPLATE.format(name=name, when=when, property_part=property_part, phone=phone)
    return base + cal_line


def _send_visit_confirmation_email(email: str, name: str, when: str, area: str, gcal: str | None, dt=None) -> None:
    try:
        from notifications.email_notifier import (
            _send, send_calendar_invite, action_bar, action_button, wa_link, PUBLIC_BASE_URL,
        )
        buttons = []
        if gcal:
            buttons.append(action_button("📅 Add to calendar", gcal, "#0f9d58"))
        buttons.append(action_button("🔄 Reschedule on WhatsApp",
                                     wa_link(f"Hi, I'd like to reschedule my visit ({when})"), "#25d366"))
        buttons.append(action_button("💬 Chat with Riya", PUBLIC_BASE_URL, "#2563eb"))
        bar = action_bar(*buttons)
        html = (f"<div style='font-family:sans-serif;max-width:560px;margin:auto;color:#0f172a'>"
                f"<p>Hi {name},</p>"
                f"<p>Your property visit is confirmed for <b>{when}</b> in {area}. "
                f"Our consultant will reach out to finalise it.</p>"
                f"{bar}"
                f"<p style='color:#64748b;font-size:13px'>Need a different time? Tap "
                f"<b>Reschedule on WhatsApp</b> above, or just reply here — we'll sort it.</p>"
                f"<p>— Riya, your property assistant 🏠</p></div>")
        plain = (f"Hi {name},\n\nYour property visit is confirmed for {when} in {area}. "
                 f"Our consultant will reach out to finalise it.\n"
                 f"Reschedule on WhatsApp: {wa_link('Reschedule my visit')}\n"
                 f"Chat with Riya: {PUBLIC_BASE_URL}\n"
                 f"{('Add to calendar: ' + gcal) if gcal else ''}\n\n— Riya")
        subject = f"Your property visit — {when}"
        if dt:
            ics = _build_ics(dt, "Property visit with Riya",
                             f"Visit in {area}. Riya will confirm by phone.", f"{area}, Lucknow", email)
            send_calendar_invite(email, subject, html, plain, ics)   # real .ics invite
        else:
            _send(email, subject, html, plain)
    except Exception as e:
        logger.warning(f"visit confirmation email failed: {e}")


def _parse_visit_time(text: str):
    """
    Best-effort, no-LLM parse of "this Saturday morning" / "tomorrow 5pm" etc.
    Returns (datetime | None, human_display_str). Falls back to the raw text when
    a concrete date/time can't be pinned (the broker confirms the exact slot anyway).
    """
    from datetime import datetime, timedelta
    now = datetime.now(IST)  # interpret the buyer's time in IST, not the server's UTC
    t = text.lower()
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    base = None
    if "day after tomorrow" in t:
        base = now + timedelta(days=2)
    elif "tomorrow" in t:
        base = now + timedelta(days=1)
    elif "today" in t or "tonight" in t:
        base = now
    else:
        for i, wd in enumerate(weekdays):
            if wd in t:
                ahead = (i - now.weekday()) % 7
                if ahead == 0:
                    ahead = 7  # "saturday" → the next one, not today
                base = now + timedelta(days=ahead)
                break

    hour = minute = None
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3) == "pm":
            hour += 12
        minute = int(m.group(2) or 0)
    elif "morning" in t:
        hour, minute = 10, 0
    elif "afternoon" in t:
        hour, minute = 15, 0
    elif "evening" in t:
        hour, minute = 18, 0
    elif "night" in t:
        hour, minute = 19, 0

    if base is not None:
        h = hour if hour is not None else 11
        mn = minute if minute is not None else 0
        dt = base.replace(hour=h, minute=mn, second=0, microsecond=0)
        if dt < now:
            dt = dt + timedelta(days=1)
        return dt, _fmt_visit(dt)
    return None, text.strip()


def _fmt_visit(dt) -> str:
    # Stored times come back as UTC; display in IST. (Naive dts from parsing are already IST.)
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(IST)
    h12 = dt.hour % 12 or 12
    ap = "am" if dt.hour < 12 else "pm"
    mm = f":{dt.minute:02d}" if dt.minute else ""
    return dt.strftime("%A, %d %b") + f" at {h12}{mm} {ap}"


def _suggest_visit_slots() -> list[dict]:
    """Three easy, concrete upcoming slots the buyer can pick from (or change)."""
    from datetime import datetime, timedelta
    now = datetime.now(IST)  # IST, so suggested slots match the buyer's clock

    def next_weekday(weekday: int, hour: int):  # 5=Sat, 6=Sun
        days = (weekday - now.weekday()) % 7
        days = days or 7  # always a FUTURE day
        return (now + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)

    tomorrow = (now + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    saturday = next_weekday(5, 16)
    sunday   = next_weekday(6, 11)
    return [{"iso": dt.isoformat(), "label": _fmt_visit(dt)} for dt in (tomorrow, saturday, sunday)]


def _handle_emi(conv: ConversationManager, user_name: str | None) -> str:
    """Give an indicative home-loan EMI for the property in focus (or the budget)."""
    cards = conv.requirements.get("_last_shown_cards") or []
    liked = conv.requirements.get("_liked_property_id")
    price = None
    if liked:
        price = next((c.get("price_inr") for c in cards if c.get("id") == liked), None)
    if not price and cards:
        price = cards[0].get("price_inr")
    if not price and conv.requirements.get("max_budget_cr"):
        price = int(conv.requirements["max_budget_cr"] * 1_00_00_000)

    name = f", {user_name}" if user_name else ""
    if not price:
        return (f"Happy to help with the numbers{name}! Tell me a property or a budget and I'll "
                "give you a rough monthly EMI. As a guide, banks fund about 80% of the price at "
                "~8.5% over 20 years.")

    # 80% loan-to-value, 8.5% p.a., 20 years.
    loan = price * 0.80
    r = 0.085 / 12
    n = 240
    emi = loan * r * (1 + r) ** n / ((1 + r) ** n - 1)
    down = price - loan

    def _money(v):
        if v >= 1_00_00_000:
            cr = v / 1_00_00_000
            return f"Rs.{cr:.2f} Cr" if cr != int(cr) else f"Rs.{int(cr)} Cr"
        lakh = v / 1_00_000
        return f"Rs.{lakh:.1f} lakh" if lakh != int(lakh) else f"Rs.{int(lakh)} lakh"

    emi_k = round(emi / 1000)
    return (
        f"Sure{name}! For a {_money(price)} property, a typical home loan (80% funded at ~8.5% "
        f"over 20 years) works out to roughly **Rs.{emi_k},000 per month**, with about "
        f"{_money(down)} as down payment. That's just indicative — I can have our consultant get "
        "you exact figures and the best bank offers. Would that help?"
    )


def _ask_for_contact(conv: ConversationManager, user_name: str | None) -> str:
    # If we already know their name (e.g. from the WhatsApp profile), don't ask for it
    # again — just request the phone number warmly.
    profile = conv.requirements.get("_profile") or {}
    if profile.get("name") and not profile.get("phone"):
        nm = profile["name"]
        return (f"Wonderful, {nm}! I'd be glad to arrange that. Could you share your "
                f"10-digit mobile number so our property consultant can reach you to set up the visit?")

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
    # Location is satisfied by an area OR a named landmark ("near Bhool Bhooliya") OR a
    # nearby place — so we don't ask "which area?" when they already gave us a place.
    has_area = bool(r.get("area") or r.get("named_landmark") or r.get("nearby"))
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
            f"Buyer just said: \"{user_message}\"\n\n"
            f"Your task: {ask_instruction}\n\n"
            "Reply like a warm, friendly human helping a friend house-hunt — NOT a form or a "
            "salesperson. ONE short, casual sentence. If their message gave real house-hunting "
            "info, acknowledge it warmly first; but if it was off-topic, unclear, or not about "
            "property, do NOT pretend to agree (never start with 'Of course!'/'Sure thing!' to "
            "something unrelated) — instead gently steer back to the home search. Vary your "
            "wording — don't reuse a phrasing from earlier in this chat, and don't tack on "
            "'...so I can find you the best options' every time. Keep it light and natural."
        ),
    })
    return _llm(messages, temperature=0.75, max_tokens=70)


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
    # If they singled out a specific property ("tell me about the 2nd one"), remember it
    # as their interested property so a later "book a visit" attaches the right listing.
    ref = _resolve_referenced_property_id(user_message, conv)
    if ref:
        conv.requirements["_liked_property_id"] = ref

    shown_text = conv.requirements.get("_last_shown_text", "")
    messages = [{"role": "system", "content": _sys(user_name)}]
    messages.append({
        "role": "user",
        "content": (
            f"The buyer is asking about properties I just showed them.\n"
            f"Their question: \"{user_message}\"\n\n"
            f"The properties currently on screen (these lines already include any distance "
            f"to a place they named, e.g. 'Phoenix United Mall: 1.17 km'):\n{shown_text}\n\n"
            "As Riya, answer their EXACT question warmly and naturally in 2-4 sentences:\n"
            "- If they ask the distance from a place, give the EXACT figure from the summary above to "
            "two decimals (e.g. 'It's 1.17 km from Phoenix United Mall as the crow flies — the actual "
            "drive may be a little more'). If they asked specifically for the 'exact' distance, give the "
            "precise number and mention it's a straight-line estimate. If no distance is listed, say you'd "
            "need to confirm it with our consultant.\n"
            "- If they ask a SUPERLATIVE ('which is the biggest/largest/cheapest/most expensive/closest?'), "
            "READ the figures in the summary and name the specific winner — e.g. biggest = highest sqft, "
            "cheapest = lowest price — and give its number (e.g. 'The biggest is Property 3, a 3 BHK at 1464 "
            "sqft'). Do not dodge the question.\n"
            "- If they're comparing, point out the key practical differences (price, size, BHK, location, "
            "standout amenity, proximity) and give a genuine recommendation.\n"
            "- If they want detail on one property, describe it warmly.\n"
            "- If they ask whether the PRICE is NEGOTIABLE or want a discount, say warmly that there's "
            "often some room to negotiate and our consultant can take it up with the owner on their "
            "behalf during the visit — do NOT invent a specific discount or new price.\n"
            "- If they ask about possession / ready-to-move / age / floor / facing / parking and it's in "
            "the summary, answer from it; if it's NOT listed, say you'll confirm that exact detail with "
            "our consultant — do not guess.\n"
            "Keep it friendly and human. End naturally (offer a visit only if it fits).\n\n"
            "⚠️ Use ONLY the facts in the summary above — never invent prices, amenities, distances, or features."
        ),
    })
    return _llm(messages, temperature=0.5, max_tokens=220)


def _card_desc(card: dict) -> str:
    bhk = card.get("bhk"); pt = (card.get("property_type") or "").title()
    head = " ".join(x for x in [f"{bhk} BHK" if bhk else "", pt] if x).strip() or "that property"
    bits = [head] + ([f"in {card['area']}"] if card.get("area") else []) + \
           ([f"({card['price_str']})"] if card.get("price_str") else [])
    return " ".join(bits)


def _handle_save_favourite(conv: ConversationManager, user_message: str, user_name: str | None) -> str:
    """Add a shown property to the buyer's favourites (by reference, else the one in focus)."""
    cards = conv.requirements.get("_last_shown_cards") or []
    nm = f", {user_name}" if user_name else ""
    if not cards:
        return (f"I'd love to save one for you{nm}! Once I've shown you some options, just say "
                f"\"save the first one\" (or 2nd, 3rd…) and I'll keep it in your favourites. ❤️")
    pid = _resolve_referenced_property_id(user_message, conv) \
        or conv.requirements.get("_liked_property_id") or cards[0].get("id")
    card = next((c for c in cards if c.get("id") == pid), cards[0])
    sl = list(conv.requirements.get("_shortlist") or [])
    if pid in sl:
        return (f"{_card_desc(card)} is already in your favourites{nm}! ❤️ "
                f"You have {len(sl)} saved — say *show favourites* to see them.")
    sl.append(pid)
    conv.requirements["_shortlist"] = sl
    return (f"Saved {_card_desc(card)} to your favourites{nm}! ❤️ You now have {len(sl)} saved. "
            f"Say *show favourites* anytime, or tell me when you'd like to visit one.")


def _show_shortlist(conv: ConversationManager, user_name: str | None) -> tuple[str, list]:
    """Return the user's saved/shortlisted properties."""
    from rag.retriever import to_card
    from database.supabase_client import get_client

    shortlist = conv.requirements.get("_shortlist", [])
    if not shortlist:
        name_part = f", {user_name}" if user_name else ""
        return (
            f"You haven't saved any properties yet{name_part}. "
            "When I show you options, just say \"save the first one\" (or 2nd, 3rd…) "
            "and I'll keep it here in your favourites. ❤️",
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
        # Make these the "in focus" set so the buyer can now say "book the first one".
        conv.requirements["_last_shown_cards"] = cards
        conv.requirements["_last_shown_text"] = format_properties_for_llm(
            [{"id": r["id"], "data": r["data"]} for r in result.data])
        if cards:
            conv.requirements["_liked_property_id"] = cards[0].get("id", "")
        name_part = f"{user_name}, here are" if user_name else "Here are"
        n = len(cards)
        return (
            f"{name_part} your {n} saved propert{'y' if n == 1 else 'ies'}. "
            f"Would you like to book a visit for {'it' if n == 1 else 'any of them'}? "
            "Just tell me which one.",
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
