"""
WhatsApp two-way broker confirmation flow.

Pipeline:
  1. Buyer books a visit → _ask_broker_availability() sends broker a WhatsApp:
       "Saubhagya Kashyap wants to visit [Property] on Saturday 21 Jun at 5 pm.
        Are you free? Reply YES to confirm or NO to suggest another time."
  2. Broker replies on WhatsApp → inbound webhook calls handle_broker_reply()
  3. YES  → save meeting, send .ics to both buyer + broker, update lead → 'scheduled'
     NO   → message the buyer: "Broker is busy that slot, please pick another time"
     reschedule text → ask broker for their available slot, relay back to buyer
"""

import logging
import re
import os
from collections import OrderedDict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Maps a broker-notification WhatsApp message-id → the customer it was about, so when
# the broker *replies to* that message ("inform him I'll call this evening") we relay to
# the right person on their own channel. In-memory (single uvicorn worker), bounded.
_NOTIFY_CTX: "OrderedDict[str, dict]" = OrderedDict()
_NOTIFY_CTX_MAX = 800


def remember_broker_notification(wamid: str, session_id, phone, name) -> None:
    if not wamid:
        return
    _NOTIFY_CTX[wamid] = {"session_id": session_id, "phone": phone, "name": name}
    _NOTIFY_CTX.move_to_end(wamid)
    while len(_NOTIFY_CTX) > _NOTIFY_CTX_MAX:
        _NOTIFY_CTX.popitem(last=False)


def context_customer(wamid: str):
    """→ (session_id, phone10, name) for the customer a replied-to notification was about."""
    ctx = _NOTIFY_CTX.get(wamid or "")
    if not ctx:
        return None
    ph = re.sub(r"\D", "", ctx.get("phone") or "")[-10:] or None
    return ctx.get("session_id"), ph, ctx.get("name")

# Natural-language YES / NO — brokers reply in sentences ("yes that works", "no sorry,
# I'm tied up then"), not just a bare word. Decline is checked FIRST (see _broker_decision).
_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|ya|haan|sure|ok(ay)?|confirm(ed)?|free|available|fine|"
    r"works|that works|works for me|sounds good|go ahead|good to go|done|perfect|👍)\b",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"\b(no(?!\s*(problem|worries|issues?))|nope|nah|nahi|sorry|busy|can'?t|cannot|"
    r"not\s*free|unavailable|tied\s*up|booked|occupied|won'?t\s*work|doesn'?t\s*work|"
    r"another\s*time|some\s*other\s*time|not\s*(that|then|available))\b",
    re.IGNORECASE,
)


def _broker_decision(text: str) -> str | None:
    """Decline beats confirm when both signals appear ('no, but yes to Monday')."""
    t = (text or "").strip()
    if _NO_RE.search(t):
        return "no"
    if _YES_RE.search(t):
        return "yes"
    return None
_RESCHEDULE_RE = re.compile(r"\b(reschedule|change.{0,10}time|different.{0,10}slot|new.{0,10}time|move.{0,10}to|shift.{0,10}to)\b", re.I)
_BROKER_PHONE  = os.environ.get("BROKER_WHATSAPP_PHONE", os.environ.get("WHATSAPP_BROKER_PHONE", ""))


def _property_label(property_id: str | None) -> str:
    """A human label for the property — '2 BHK Flat in Gomtinagar Ext · Rs.36 lakh'."""
    if not property_id:
        return "a property"
    try:
        from database.supabase_client import get_client
        rows = get_client().table("properties").select("data").eq("id", property_id).limit(1).execute().data
        if not rows:
            return f"Property {property_id}"
        d = rows[0].get("data") or {}
        prof = d.get("property_profile", {}); loc = d.get("location", {}); pr = d.get("pricing", {})
        bhk = prof.get("bhk"); ptype = (prof.get("property_type") or "").title(); area = loc.get("area_name") or ""
        price = pr.get("total_price_inr")
        if price:
            if price >= 1_00_00_000:
                cr = price / 1_00_00_000; price_s = f"Rs.{cr:.2g} Cr"
            else:
                price_s = f"Rs.{price/100000:.0f} lakh"
        else:
            price_s = ""
        head = " ".join(x for x in [f"{bhk} BHK" if bhk else "", ptype] if x).strip()
        bits = [b for b in [head, (f"in {area}" if area else ""), price_s] if b]
        return " · ".join(bits) or f"Property {property_id}"
    except Exception:
        return f"Property {property_id}"


def _day_schedule_note(proposed_dt: datetime | None) -> str:
    """Tell the broker how busy that day already is, so they have context."""
    if not proposed_dt:
        return ""
    try:
        from datetime import timedelta
        from database.supabase_client import get_client
        day_start = proposed_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        rows = (get_client().table("meetings").select("id,status,scheduled_at")
                .gte("scheduled_at", day_start.isoformat()).lt("scheduled_at", day_end.isoformat())
                .execute().data) or []
        active = [r for r in rows if (r.get("status") or "").lower() not in ("cancelled",)]
        n = len(active)
        if n == 0:
            return "🗓️ Your day looks clear so far."
        return f"🗓️ You already have *{n}* visit(s) booked that day."
    except Exception:
        return ""


def is_configured_broker(phone: str) -> bool:
    """True if this WhatsApp number is the configured broker."""
    configured = os.environ.get("BROKER_WHATSAPP_PHONE") or os.environ.get("WHATSAPP_BROKER_PHONE") or ""
    def _n(p): return re.sub(r"\D", "", p or "")[-10:]
    return bool(configured) and _n(phone) == _n(configured)


def _fmt_when(iso: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("Asia/Kolkata"))  # show IST, not UTC
        h = dt.hour % 12 or 12
        ap = "am" if dt.hour < 12 else "pm"
        mm = f":{dt.minute:02d}" if dt.minute else ""
        return dt.strftime("%a, %d %b") + f" at {h}{mm} {ap}"
    except Exception:
        return str(iso)[:16]


def _broker_meetings_summary() -> str:
    """List the broker's upcoming (non-cancelled) visits with customer + property."""
    from database.supabase_client import get_client
    from datetime import datetime, timezone
    try:
        c = get_client()
        now = datetime.now(timezone.utc).isoformat()
        rows = (c.table("meetings").select("*, leads(name,phone)")
                .gte("scheduled_at", now).neq("status", "cancelled")
                .order("scheduled_at").limit(10).execute().data) or []
    except Exception as e:
        logger.warning(f"broker meetings summary failed: {e}")
        return "Couldn't fetch your visits right now — please try again in a moment."
    if not rows:
        return "📋 You have no upcoming visits scheduled right now."
    lines = ["📋 *Your upcoming visits:*"]
    for m in rows:
        lead = m.get("leads") or {}
        nm = lead.get("name") or "Customer"
        ph = lead.get("phone") or ""
        status = (m.get("status") or "").lower()
        tag = "✅" if status == "confirmed" else "⏳"
        prop = _property_label(m.get("property_id"))
        lines.append(f"{tag} *{_fmt_when(m.get('scheduled_at'))}* — {nm} ({ph})\n     🏠 {prop}")
    return "\n".join(lines)


def _broker_help_menu() -> str:
    return ("👋 *Here's what I can do for you*\n"
            "• *meetings* — your upcoming visits\n"
            "• *stats* — leads, listings & visit numbers\n"
            "• Reply *YES* / *NO* to a visit request I send you\n"
            "• *reschedule 9876543210 to Friday 5pm*\n"
            "• *message Ravi: I'll call you this evening*\n"
            "• *add 2 BHK flat in Gomti Nagar, 45 lakh, 1100 sqft, lift & parking*\n\n"
            "Or just talk to me normally — I'll help. 🏠")


def _broker_stats() -> str:
    """Live counts across the business — grounded in the DB, never invented."""
    from database.supabase_client import get_client
    from datetime import datetime, timezone
    c = get_client()

    def _count(table, **filters):
        try:
            q = c.table(table).select("id", count="exact")
            for k, v in filters.items():
                q = q.eq(k, v)
            return q.execute().count
        except Exception:
            return None

    leads = _count("leads")
    props = _count("properties", status="available")
    try:
        now = datetime.now(timezone.utc).isoformat()
        visits = (c.table("meetings").select("id", count="exact")
                  .gte("scheduled_at", now).neq("status", "cancelled").execute().count)
    except Exception:
        visits = None

    parts = ["📊 *Your dashboard*"]
    if leads is not None:
        parts.append(f"👥 Leads (customers): *{leads}*")
    if props is not None:
        parts.append(f"🏠 Live listings: *{props}*")
    if visits is not None:
        parts.append(f"📅 Upcoming visits: *{visits}*")
    parts.append("\nType *meetings* for the visit details.")
    return "\n".join(parts)


# ── Multi-channel customer delivery ──────────────────────────────────────────
# A customer's session_id tells us where to reach them:
#   wa_<phone>  → WhatsApp     web_<id> → Website (queued, pull-based)
#   <all digits> → Telegram chat_id
_PRONOUN_RE = re.compile(
    r"^\s*(him|her|them|it|that (guy|person|man|woman|one|customer|lead|client)|"
    r"the (customer|buyer|lead|client|guy|person|man|woman|one)|"
    r"this (customer|lead|person|one))\s*$", re.I)


def _channel_of(session_id: str) -> str:
    sid = (session_id or "").strip()
    if sid.startswith("wa_"):
        return "whatsapp"
    if sid.startswith("web_"):
        return "web"
    if sid.isdigit():
        return "telegram"
    return "unknown"


def _send_telegram(chat_id: str, text: str) -> bool:
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    try:
        cid = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": cid, "text": text}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram relay failed: {e}")
        return False


def _queue_web_message(session_id: str, message: str) -> bool:
    """Web is pull-based — stash the message so it's shown on the customer's next
    message or page reload (process flushes _broker_msgs)."""
    from database.supabase_client import get_session, save_session
    try:
        s = get_session(session_id)
        req = s.get("requirements") or {}
        q = req.get("_broker_msgs") or []
        q.append(message)
        req["_broker_msgs"] = q
        save_session(session_id, s.get("messages") or [], req, s.get("stage") or "discovery")
        return True
    except Exception as e:
        logger.warning(f"web queue failed: {e}")
        return False


def notify_customer(session_id: str, phone: str, message: str) -> tuple[bool, str]:
    """Deliver a message to a customer on THEIR channel. Returns (ok, channel)."""
    ch = _channel_of(session_id)
    if ch == "telegram":
        return _send_telegram(session_id, message), "Telegram"
    if ch == "web":
        return _queue_web_message(session_id, message), "the website"
    # whatsapp, or unknown-but-we-have-a-phone
    digits = re.sub(r"\D", "", phone or "")
    if digits:
        from notifications.whatsapp_notifier import _send
        return _send(digits[-10:] if len(digits) >= 10 else digits, message), "WhatsApp"
    return False, "their channel"


def _resolve_customer(target: str, reply_context_id: str | None = None):
    """Resolve who the broker means → (session_id, phone10, name).
    Priority: explicit phone → explicit name → the message they REPLIED to →
    (a bare pronoun with no reply context) give up so we can ask. We never guess
    'the most recent lead'."""
    from database.supabase_client import get_client
    t = (target or "").strip()
    digits = re.sub(r"\D", "", t)
    c = get_client()

    def _row(r):
        return (r.get("session_id"),
                re.sub(r"\D", "", r.get("phone") or "")[-10:] or None,
                r.get("name"))

    if len(digits) >= 10:                                   # explicit phone
        ph = digits[-10:]
        try:
            rows = (c.table("leads").select("name,phone,session_id")
                    .ilike("phone", f"%{ph}%").order("created_at", desc=True)
                    .limit(1).execute().data) or []
        except Exception:
            rows = []
        return _row(rows[0]) if rows else (None, ph, None)

    if t and not _PRONOUN_RE.match(t):                      # explicit name
        try:
            rows = (c.table("leads").select("name,phone,session_id")
                    .ilike("name", f"%{t}%").order("created_at", desc=True)
                    .limit(1).execute().data) or []
        except Exception:
            rows = []
        if rows:
            return _row(rows[0])

    if reply_context_id:                                    # the notification they replied to
        ctx = context_customer(reply_context_id)
        if ctx:
            return ctx

    return (None, None, None)


def _broker_message_customer(text: str, reply_context_id: str | None = None):
    """Broker asks us to relay a message to a customer, on the customer's OWN channel
    (WhatsApp / Telegram / website). Returns reply str, or None if it isn't a relay."""
    import json
    from agent.llm_client import complete
    try:
        js = complete([
            {"role": "system", "content": (
                "A real-estate broker wants to send a message to one of their customers. "
                "Extract it. Return ONLY JSON: {\"target\":\"the customer's name or phone, or a "
                "pronoun like 'him'/'her'/'them' if they didn't name anyone\","
                "\"message\":\"the message to send, rewritten in first person from the broker to the "
                "customer (e.g. 'I'll call you this evening')\"}. "
                "If the broker is NOT asking to message a customer, return {\"target\":\"\",\"message\":\"\"}.")},
            {"role": "user", "content": text},
        ], temperature=0, max_tokens=160, json_mode=True)
        data = json.loads(js)
    except Exception:
        data = {}
    target = (data.get("target") or "").strip()
    message = (data.get("message") or "").strip()
    if not message:
        return None

    session_id, phone, name = _resolve_customer(target, reply_context_id)
    if not session_id and not phone:
        return ("Who should I message? *Reply to* the customer's lead/visit message, "
                "or name them — e.g.\n*message Ravi: I'll call you this evening* "
                "or *tell 9876543210 …*")

    first = (name or "").split()[0] if name else ""
    body = (f"Hi{(' ' + first) if first else ''} 👋 A message from your Riya property consultant:\n\n"
            f"{message}")
    ok, channel = notify_customer(session_id, phone, body)
    who = name or phone or "them"
    if ok:
        return f"✅ Sent to *{who}* on {channel}:\n_{message}_"
    return (f"I couldn't reach *{who}* on {channel} right now"
            + (" — WhatsApp only allows messaging within 24 h of their last message."
               if channel == "WhatsApp" else ".")
            + " You may need to contact them directly.")


def _broker_add_property_from_text(text: str, broker_phone: str) -> str:
    """Broker adds a listing in natural language → parsed → created & made live."""
    import json
    from agent.llm_client import complete
    try:
        js = complete([
            {"role": "system", "content": (
                "Extract a property listing from a broker's message. Return ONLY JSON with keys: "
                "property_type (one of: Flat, Independent House, Villa, Builder Floor, Plot, Shop, Office), "
                "bhk (integer or null), price_inr (integer rupees — convert '45 lakh'->4500000, "
                "'1.2 cr'->12000000), area_sqft (number or null), "
                "furnishing (Furnished|Semi-Furnished|Unfurnished or null), "
                "address (the locality/area text, e.g. 'Gomti Nagar'), city (default 'Lucknow'), "
                "amenities (comma-separated string or null). Use null when a field isn't stated.")},
            {"role": "user", "content": text},
        ], temperature=0, max_tokens=250, json_mode=True)
        data = json.loads(js)
    except Exception:
        data = {}
    ptype = data.get("property_type")
    price = data.get("price_inr")
    addr = data.get("address")
    if not ptype or not price or not addr:
        return ("To add a listing I need at least the type, price and area — e.g.\n"
                "*Add 2 BHK flat in Gomti Nagar, 45 lakh, 1100 sqft, semi-furnished, lift and parking*")
    fields = {
        "property_type": ptype,
        "bhk": data.get("bhk"),
        "price_inr": int(price),
        "area_sqft": data.get("area_sqft"),
        "furnishing": data.get("furnishing"),
        "address": addr,
        "city": data.get("city") or "Lucknow",
        "amenities": data.get("amenities"),
        "broker_phone": broker_phone,
    }
    try:
        from broker.upload_handler import create_property_from_fields
        result = create_property_from_fields(fields, broker_id=f"wa_{broker_phone}")
    except Exception as e:
        logger.error(f"broker WA add-property failed: {e}")
        return "Something went wrong adding that listing. Please try the dashboard → Add Property."
    if result.get("ok"):
        label = _property_label(result.get("property_id"))
        return (f"✅ Added *{label}* — it's live for buyers now.\n"
                "Add photos any time from the dashboard → My Listings.")
    return f"Couldn't add it: {result.get('error', 'please check the details and try again')}"


def _broker_assistant_llm(broker_phone: str, text: str) -> str:
    """Conversational broker assistant — replaces the old canned greeting."""
    from agent.llm_client import complete
    system = (
        "You are Riya, the WhatsApp assistant for a real-estate BROKER (your teammate, not a buyer) "
        "in Lucknow. Help them run their business. Reply in a warm, short WhatsApp style "
        "(1-3 sentences, minimal emoji). When relevant, tell them the exact command to use:\n"
        "• 'meetings' — upcoming visits\n"
        "• 'stats' — leads, listings, visits\n"
        "• Reply YES/NO to a visit request I send\n"
        "• 'reschedule <buyer phone> to <day time>'\n"
        "• 'message <name/phone>: <text>' — I'll text that customer for you\n"
        "• 'add <bhk> <type> in <area>, <price>, <sqft>, <furnishing>, <amenities>' — I'll list it\n"
        "You handle real-estate tasks only — you cannot generate images or do unrelated things; "
        "say so briefly and steer back. Never invent numbers; if asked for counts, tell them to type "
        "'stats' or 'meetings'."
    )
    try:
        return complete(
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.5, max_tokens=200,
        )
    except Exception:
        return _broker_help_menu()


# Intent signals for the broker command router
_STATS_RE = re.compile(
    r"\b(stats?|statistics|numbers|overview|dashboard|performance)\b", re.I)
_HOWMANY_RE = re.compile(r"\b(how many|number of|count|total)\b", re.I)
_BIZ_NOUN_RE = re.compile(
    r"\b(customer|lead|buyer|client|propert|listing|flat|home|visit|meeting|booking)", re.I)
_MEETINGS_RE = re.compile(
    r"\b(meetings?|my visits?|appointments?|bookings?|upcoming|agenda|schedule)\b", re.I)
_HELP_RE = re.compile(r"\b(help|menu|commands?|what can you|options?)\b", re.I)
_ADD_PROP_RE = re.compile(
    r"\b(add|list|create|post|put up|register)\b.{0,20}\b(propert|listing|flat|villa|plot|house|home|bhk|shop|office)\b", re.I)
_MSG_VERB_RE = re.compile(
    r"\b(message|msg|text|tell|notify|relay|drop (a|him|her|them)|let .* know|send (a )?(message|msg|text|note))\b", re.I)
_ASK_SELF_RE = re.compile(r"\b(tell|inform|remind|show|let|give) me\b", re.I)


def handle_broker_command(broker_phone: str, text: str, reply_context_id: str | None = None) -> str:
    """The broker messaged something that isn't a YES/NO confirmation reply.
    Routes to a real capability, else answers conversationally via the LLM.
    reply_context_id: the WhatsApp message the broker replied to (if any)."""
    t = (text or "").strip()
    tl = t.lower()
    if not tl:
        return _broker_help_menu()

    if _MEETINGS_RE.search(tl):
        return _broker_meetings_summary()
    if _STATS_RE.search(tl) or (_HOWMANY_RE.search(tl) and _BIZ_NOUN_RE.search(tl)):
        return _broker_stats()
    if _HELP_RE.search(tl):
        return _broker_help_menu()
    if _ADD_PROP_RE.search(tl):
        return _broker_add_property_from_text(t, broker_phone)
    # Relay a message to a customer. Treat it as a relay if it's clearly a message verb
    # (not "tell me …"), OR the broker is replying to a customer's lead/visit notification.
    if reply_context_id or (_MSG_VERB_RE.search(tl) and not _ASK_SELF_RE.search(tl)):
        relayed = _broker_message_customer(t, reply_context_id)
        if relayed is not None:
            return relayed
    # Anything else — talk to them like a real assistant.
    return _broker_assistant_llm(broker_phone, t)


def ask_broker_availability(
    buyer_name: str,
    buyer_phone: str,
    buyer_session_id: str,
    proposed_when: str,
    proposed_dt: datetime | None,
    property_id: str | None,
    lead_id: str | None,
    meeting_id: str | None,
    broker_phone: str,
) -> bool:
    """
    Ask the broker (WhatsApp + email) if they're free for the proposed slot, with the
    customer's details, the actual property, and that day's existing appointments.
    Stores a pending confirmation so we can match their reply. Returns True if WA sent.
    """
    from notifications.whatsapp_notifier import _send_get_id
    from database.supabase_client import save_broker_confirmation, get_broker_by_phone

    prop_label = _property_label(property_id)
    day_note = _day_schedule_note(proposed_dt)
    msg = (
        f"🏠 *New visit request*\n\n"
        f"*{buyer_name}* wants to visit:\n_{prop_label}_\n"
        f"📅 *{proposed_when}*\n"
        f"📞 Buyer: {buyer_phone}\n"
        f"{day_note}\n\n"
        f"Are you free then?\n"
        f"Reply *YES* to confirm, *NO* if busy, "
        f"or just send a better time (e.g. _Friday 4pm_)."
    )

    wamid = _send_get_id(broker_phone, msg)
    if wamid is None:
        logger.warning(f"Could not send broker confirmation WA to {broker_phone}")
        return False
    # So a broker who *replies* to this request can also relay to this buyer.
    if wamid:
        remember_broker_notification(wamid, buyer_session_id, buyer_phone, buyer_name)

    # Email the broker too (best-effort) so it's on record even if they miss WhatsApp.
    try:
        broker = get_broker_by_phone(broker_phone) or {}
        bemail = broker.get("email")
        if bemail:
            from notifications.email_notifier import (
                _send as _email_send, action_bar, action_button, PUBLIC_BASE_URL,
            )
            bdigits = "".join(ch for ch in (buyer_phone or "") if ch.isdigit())[-10:]
            bar = action_bar(
                action_button(f"💬 WhatsApp {buyer_name}", f"https://wa.me/91{bdigits}", "#25d366"),
                action_button("📊 Manage on dashboard", f"{PUBLIC_BASE_URL}/broker/meetings", "#2563eb"),
            )
            subj = f"Visit request: {buyer_name} — {proposed_when}"
            html = (f"<div style='font-family:sans-serif;max-width:560px;margin:auto;color:#0f172a'>"
                    f"<p><b>New visit request</b></p>"
                    f"<p><b>{buyer_name}</b> wants to visit <i>{prop_label}</i><br>"
                    f"📅 <b>{proposed_when}</b><br>📞 {buyer_phone}</p>"
                    f"<p style='color:#64748b'>{day_note}</p>"
                    f"<p>Reply on WhatsApp: <b>YES</b> to confirm, <b>NO</b> if busy, or send a better time.</p>"
                    f"{bar}</div>")
            plain = (f"New visit request\n\n{buyer_name} wants to visit {prop_label}\n"
                     f"{proposed_when}\nBuyer: {buyer_phone}\n{day_note}\n\n"
                     f"Reply on WhatsApp: YES / NO / a better time.\n"
                     f"Dashboard: {PUBLIC_BASE_URL}/broker/meetings")
            _email_send(bemail, subj, html, plain)
    except Exception as e:
        logger.debug(f"broker availability email skipped: {e}")

    try:
        save_broker_confirmation({
            "broker_phone": broker_phone,
            "buyer_name": buyer_name,
            "buyer_phone": buyer_phone,
            "buyer_session_id": buyer_session_id,
            "property_id": property_id,
            "lead_id": lead_id,
            "meeting_id": meeting_id,
            "proposed_dt": proposed_dt.isoformat() if proposed_dt else None,
            "proposed_when": proposed_when,
        })
    except Exception as e:
        logger.error(f"Could not save broker_confirmation record: {e}")

    logger.info(f"Broker availability check sent to {broker_phone} for {proposed_when}")
    return True


def handle_broker_reply(broker_phone: str, reply_text: str) -> bool:
    """
    Called when broker replies on WhatsApp. Matches to a pending confirmation,
    then either books the meeting or informs the buyer.
    Returns True if we handled it (so the general WA handler doesn't also process it).
    """
    from database.supabase_client import (
        get_pending_broker_confirmation, update_broker_confirmation,
        save_meeting, update_meeting, update_lead,
    )
    from notifications.whatsapp_notifier import _send
    from notifications.email_notifier import _send as _email_send
    from agent.property_agent import _build_ics, _gcal_link, _send_visit_confirmation_email

    # ── Broker-identity guard ────────────────────────────────────────────────
    # This handler runs FIRST for every inbound WhatsApp message. Only let it act when
    # the sender is actually the broker — otherwise a buyer who types "reschedule" or
    # "yes" gets pulled into the broker flow (and shown broker-only instructions).
    def _norm(p): return re.sub(r"\D", "", p or "")[-10:]
    configured = os.environ.get("BROKER_WHATSAPP_PHONE") or os.environ.get("WHATSAPP_BROKER_PHONE") or ""
    is_broker = bool(configured) and _norm(broker_phone) == _norm(configured)
    if not is_broker:
        try:
            is_broker = bool(get_pending_broker_confirmation(broker_phone))
        except Exception:
            is_broker = False
    if not is_broker:
        return False  # sender isn't the broker — let the buyer agent handle it

    if _RESCHEDULE_RE.search(reply_text):
        return handle_broker_reschedule(broker_phone, reply_text)

    conf = get_pending_broker_confirmation(broker_phone)
    if not conf:
        return False  # not a pending confirmation reply — let normal agent handle it

    conf_id   = conf["id"]
    buyer_phone   = conf.get("buyer_phone", "")
    buyer_name    = conf.get("buyer_name", "")
    buyer_sid     = conf.get("buyer_session_id", "")
    proposed_when = conf.get("proposed_when", "the agreed time")
    proposed_dt_s = conf.get("proposed_dt")
    property_id   = conf.get("property_id")
    lead_id       = conf.get("lead_id")
    meeting_id    = conf.get("meeting_id")

    proposed_dt = None
    if proposed_dt_s:
        try:
            proposed_dt = datetime.fromisoformat(proposed_dt_s)
        except Exception:
            pass

    _decision = _broker_decision(reply_text)
    if _decision == "yes":
        # Broker is free → book the meeting
        meeting_fields = {
            "property_id": property_id,
            "status": "confirmed",
            "notes": f"Broker confirmed via WhatsApp for {proposed_when}",
        }
        if proposed_dt:
            meeting_fields["scheduled_at"] = proposed_dt.isoformat()
        if lead_id:
            meeting_fields["lead_id"] = lead_id

        try:
            if meeting_id:
                update_meeting(meeting_id, meeting_fields)
            else:
                saved = save_meeting(meeting_fields)
                meeting_id = (saved or {}).get("id")
                if meeting_id:
                    update_broker_confirmation(conf_id, fields={"meeting_id": meeting_id})
            if lead_id:
                update_lead(lead_id, {"status": "visit"})
            update_broker_confirmation(conf_id, "yes")
        except Exception as e:
            logger.error(f"save_meeting failed after broker YES: {e}")
            _send(broker_phone, "I couldn't confirm that visit right now. Please reply YES again shortly.")
            return True

        # Add event to broker's Google Calendar (if configured)
        gcal_event_link = None
        try:
            from notifications.calendar_client import add_event_to_broker_calendar
            gcal_event_link = add_event_to_broker_calendar(
                proposed_dt,
                summary=f"Property visit — {buyer_name}",
                description=f"Buyer: {buyer_name} | Phone: {buyer_phone} | Property: {property_id or 'TBD'}",
                duration_minutes=60,
            )
        except Exception as _e:
            logger.debug(f"Google Calendar event skipped: {_e}")

        # WhatsApp the broker confirmation
        broker_gcal = _gcal_link(proposed_dt, "Property visit", f"Visit with {buyer_name}", "Lucknow")
        broker_calendar = f"\nAdd it to your calendar: {broker_gcal}" if broker_gcal else ""
        _send(broker_phone, f"Confirmed! I've booked the visit for *{proposed_when}*. "
                            f"The buyer will be informed.{broker_calendar}")
        _send_broker_calendar_invite(broker_phone, buyer_name, proposed_when, proposed_dt, broker_gcal)

        # Notify the buyer on THEIR channel (WhatsApp / Telegram / website) + SMS
        buyer_msg = (
            f"Great news, {buyer_name}! Your visit has been confirmed for *{proposed_when}*. "
            f"Our consultant will be there — see you!"
        )
        notify_customer(buyer_sid, buyer_phone, buyer_msg)
        if buyer_phone:
            try:
                from notifications.sms_notifier import send_visit_sms_buyer
                send_visit_sms_buyer(buyer_phone, buyer_name, proposed_when)
            except Exception as _e:
                logger.debug(f"SMS on YES skipped: {_e}")

        # Email .ics invite to buyer if their session has an email
        if buyer_sid:
            try:
                from database.supabase_client import get_session
                sess = get_session(buyer_sid)
                buyer_email = ((sess.get("requirements") or {}).get("_profile") or {}).get("email")
                if buyer_email and proposed_dt:
                    gcal = _gcal_link(proposed_dt, "Property visit", f"Visit on {proposed_when}", "Lucknow")
                    _send_visit_confirmation_email(buyer_email, buyer_name, proposed_when,
                                                   "Lucknow", gcal, proposed_dt)
            except Exception as e:
                logger.warning(f"Could not send buyer email after broker YES: {e}")

        logger.info(f"Meeting confirmed by broker {broker_phone} for {proposed_when}")
        return True

    elif _decision == "no":
        # Broker is busy → inform buyer, ask them to pick another time
        update_broker_confirmation(conf_id, "no")
        if meeting_id:
            update_meeting(meeting_id, {
                "status": "cancelled",
                "notes": f"Broker declined requested slot: {proposed_when}",
            })
        _prepare_buyer_whatsapp_reschedule(conf)
        _send(broker_phone, "Got it — I'll let the buyer know and ask them to pick another time.")
        notify_customer(buyer_sid, buyer_phone,
                        f"Hi {buyer_name}, our consultant is busy at *{proposed_when}*. "
                        "Could you suggest another day/time? I'll check availability right away.")
        logger.info(f"Broker {broker_phone} declined {proposed_when}")
        return True

    # Any other text — check if broker is trying to reschedule
    if _RESCHEDULE_RE.search(reply_text):
        return handle_broker_reschedule(broker_phone, reply_text, conf)

    # Otherwise don't consume it; let the normal agent handle it.
    return False


def handle_broker_reschedule(broker_phone: str, text: str, conf: dict | None = None) -> bool:
    """
    Broker texts something like "reschedule Arjun to Friday 4pm" or
    "change time to Thursday 6pm". We parse the new time, update the meeting,
    resend .ics to the buyer, and confirm to the broker.
    Returns True if we handled it.
    """
    from notifications.whatsapp_notifier import _send
    from database.supabase_client import (
        get_latest_broker_confirmation, update_broker_confirmation, update_meeting,
    )
    from agent.property_agent import _parse_visit_time, _build_ics, _gcal_link, _send_visit_confirmation_email

    # Try to find the most recent confirmed meeting for this broker
    if conf is None:
        phone_match = re.search(r"(?<!\d)(?:91)?([6-9]\d{9})(?!\d)", text)
        buyer_phone_hint = phone_match.group(1) if phone_match else None
        conf = get_latest_broker_confirmation(broker_phone, buyer_phone_hint)

    if not conf:
        # Try looking up any recent meeting associated with this broker
        # (fall back to a generic "tell me the buyer name / new time" prompt)
        _send(broker_phone,
              "Sure — to reschedule, please reply:\n"
              "*RESCHEDULE [buyer phone] to [new day and time]*\n"
              "e.g. RESCHEDULE 9876543210 to Friday 5pm")
        return True

    dt, when = _parse_visit_time(text)
    if not dt:
        _send(broker_phone,
              "I couldn't read the new time. Please reply like:\n"
              "*Thursday 4pm* or *25 Jun at 6 pm*")
        return True

    meeting_id = conf.get("meeting_id")
    buyer_phone = conf.get("buyer_phone","")
    buyer_name  = conf.get("buyer_name","the buyer")
    buyer_sid   = conf.get("buyer_session_id","")
    proposed_when = when

    if not meeting_id:
        _send(broker_phone, "I found the visit details but not its meeting record. Please reschedule it from the dashboard.")
        return True

    try:
        update_meeting(meeting_id, {
            "scheduled_at": dt.isoformat(),
            "status": "confirmed",
            "notes": f"Broker rescheduled via WhatsApp to {proposed_when}",
        })
        update_broker_confirmation(
            conf["id"], "rescheduled",
            {"proposed_dt": dt.isoformat(), "proposed_when": proposed_when},
        )
    except Exception as e:
        logger.error(f"update_meeting on reschedule: {e}")
        _send(broker_phone, "I couldn't save that new time. Please try again shortly.")
        return True

    # Notify buyer on their own channel
    notify_customer(buyer_sid, buyer_phone,
                    f"Hi {buyer_name}, your property visit has been rescheduled to *{proposed_when}*. "
                    "Same property — see you there!")

    # Email buyer new .ics if we have their email
    if buyer_sid:
        try:
            from database.supabase_client import get_session
            sess = get_session(buyer_sid)
            buyer_email = ((sess.get("requirements") or {}).get("_profile") or {}).get("email")
            if buyer_email:
                gcal = _gcal_link(dt, "Property visit", f"Rescheduled visit on {proposed_when}", "Lucknow")
                _send_visit_confirmation_email(buyer_email, buyer_name, proposed_when, "Lucknow", gcal, dt)
        except Exception as e:
            logger.warning(f"Could not email buyer on reschedule: {e}")

    broker_gcal = _gcal_link(dt, "Property visit", f"Visit with {buyer_name}", "Lucknow")
    _send(broker_phone,
          f"Done! Rescheduled to *{proposed_when}*. "
          f"{buyer_name} has been notified on WhatsApp.\n"
          f"Add it to your calendar: {broker_gcal}")
    logger.info(f"Broker {broker_phone} rescheduled meeting {meeting_id} to {proposed_when}")
    return True


def _prepare_buyer_whatsapp_reschedule(conf: dict) -> None:
    """Make the buyer's next WhatsApp message act as the replacement visit time."""
    buyer_phone = conf.get("buyer_phone")
    meeting_id = conf.get("meeting_id")
    if not buyer_phone or not meeting_id:
        return

    from database.supabase_client import get_session, save_session, _normalize_indian_phone

    # Prefer the exact session we stored when asking the broker — the buyer's WhatsApp
    # session is keyed by their WhatsApp number, which may differ from the phone they
    # typed for the lead. Reconstructing from the typed phone primes the wrong session.
    session_id = conf.get("buyer_session_id") or f"wa_{_normalize_indian_phone(buyer_phone)}"
    session = get_session(session_id)
    requirements = session.get("requirements") or {}
    profile = requirements.get("_profile") or {}
    profile.update({"name": conf.get("buyer_name"), "phone": buyer_phone})
    requirements["_profile"] = profile
    requirements["_pending_meeting"] = {
        "meeting_id": meeting_id,
        "lead_id": conf.get("lead_id"),
        "property_id": conf.get("property_id"),
        "phone": buyer_phone,
        "name": conf.get("buyer_name"),
        "reschedule": True,
    }
    requirements["_last_meeting_id"] = meeting_id
    save_session(session_id, session.get("messages") or [], requirements, "scheduling")


def _send_broker_calendar_invite(broker_phone: str, buyer_name: str, when: str,
                                 dt: datetime | None, gcal: str | None) -> None:
    """Email the broker an ICS invite when their broker profile has an email."""
    if not dt:
        return
    try:
        from database.supabase_client import get_broker_by_phone
        from notifications.email_notifier import send_calendar_invite
        from agent.property_agent import _build_ics

        broker = get_broker_by_phone(broker_phone) or {}
        email = broker.get("email")
        if not email:
            return
        broker_name = broker.get("name") or "there"
        subject = f"Property visit with {buyer_name} - {when}"
        plain = (f"Hi {broker_name},\n\nYour property visit with {buyer_name} is confirmed "
                 f"for {when}.\n{('Add to Google Calendar: ' + gcal) if gcal else ''}")
        calendar_html = f'<p><a href="{gcal}">Add to Google Calendar</a></p>' if gcal else ""
        html = (f"<p>Hi {broker_name},</p><p>Your property visit with <b>{buyer_name}</b> "
                f"is confirmed for <b>{when}</b>.</p>{calendar_html}")
        ics = _build_ics(dt, f"Property visit with {buyer_name}",
                         "Broker-confirmed property visit", "Lucknow", email)
        send_calendar_invite(email, subject, html, plain, ics)
    except Exception as e:
        logger.warning(f"Could not send broker calendar invite: {e}")
