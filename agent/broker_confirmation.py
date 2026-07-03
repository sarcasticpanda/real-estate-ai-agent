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
# the right person on their own channel. Kept in memory for speed AND persisted to disk
# so it survives restarts/redeploys (single uvicorn worker).
import json as _json
from pathlib import Path as _Path
import threading as _threading

_NOTIFY_CTX: "OrderedDict[str, dict]" = OrderedDict()
_NOTIFY_CTX_MAX = 800
_NOTIFY_CTX_TTL = 36 * 3600  # a reply beyond ~1.5 days almost certainly isn't about this
_CTX_FILE = _Path(__file__).resolve().parents[1] / "runtime" / "notify_ctx.json"
_CTX_LOCK = _threading.Lock()


def _ctx_load() -> None:
    try:
        data = _json.loads(_CTX_FILE.read_text(encoding="utf-8"))
        for k, v in data.items():
            _NOTIFY_CTX[k] = v
    except Exception:
        pass


def _ctx_persist() -> None:
    try:
        _CTX_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CTX_FILE.write_text(_json.dumps(dict(_NOTIFY_CTX), ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug(f"notify_ctx persist failed: {e}")


_ctx_load()


def remember_broker_notification(wamid: str, session_id, phone, name) -> None:
    if not wamid:
        return
    with _CTX_LOCK:
        _NOTIFY_CTX[wamid] = {"session_id": session_id, "phone": phone, "name": name,
                              "ts": datetime.now(timezone.utc).timestamp()}
        _NOTIFY_CTX.move_to_end(wamid)
        while len(_NOTIFY_CTX) > _NOTIFY_CTX_MAX:
            _NOTIFY_CTX.popitem(last=False)
        _ctx_persist()


def context_customer(wamid: str):
    """→ (session_id, phone10, name) for the customer a replied-to notification was about."""
    ctx = _NOTIFY_CTX.get(wamid or "")
    if not ctx:
        return None
    ts = ctx.get("ts")
    if ts and (datetime.now(timezone.utc).timestamp() - ts) > _NOTIFY_CTX_TTL:
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
        return "You have no upcoming visits scheduled right now."
    lines = ["*Upcoming visits*"]
    for m in rows:
        lead = m.get("leads") or {}
        nm = lead.get("name") or "Customer"
        ph = lead.get("phone") or ""
        status = (m.get("status") or "").lower()
        mark = "✓" if status == "confirmed" else "·"
        prop = _property_label(m.get("property_id"))
        lines.append(f"{mark} *{_fmt_when(m.get('scheduled_at'))}* — {nm} ({ph})\n   {prop}")
    return "\n".join(lines)


def _broker_help_menu() -> str:
    return ("*What I can do*\n"
            "• *meetings* — your upcoming visits\n"
            "• *stats* — leads, listings & visit numbers\n"
            "• *show new leads* / *who is negotiating* / *find Ravi* — search your pipeline\n"
            "• *move Ravi to negotiating* / *put 9876543210 on hold* — move a lead\n"
            "• *ask Ravi if he's free Saturday 5pm* — I ask & auto-reschedule if he agrees\n"
            "• Reply *YES* / *NO* to a visit request I send you\n"
            "• *reschedule 9876543210 to Friday 5pm*\n"
            "• *message Ravi: I'll call you this evening* — I text them + loop you on replies\n"
            "• *add 2 BHK flat in Gomti Nagar, 45 lakh, 1100 sqft, lift & parking*\n\n"
            "Or just tell me what you need in your own words.")


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

    parts = ["*Your numbers*"]
    if leads is not None:
        parts.append(f"• Leads: *{leads}*")
    if props is not None:
        parts.append(f"• Live listings: *{props}*")
    if visits is not None:
        parts.append(f"• Upcoming visits: *{visits}*")
    parts.append("\nType *meetings* for details.")
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


def _mark_relay_open(session_id: str, broker_phone: str) -> None:
    """Flag a customer's session so their next reply is looped back to the broker."""
    if not session_id or not broker_phone:
        return
    from database.supabase_client import get_session, save_session
    try:
        s = get_session(session_id)
        req = s.get("requirements") or {}
        req["_relay_broker"] = broker_phone
        save_session(session_id, s.get("messages") or [], req, s.get("stage") or "discovery")
    except Exception as e:
        logger.debug(f"mark relay open failed: {e}")


def forward_customer_reply_to_broker(broker_phone: str, session_id: str,
                                     requirements: dict, text: str) -> None:
    """Two-way loop: after the broker messaged a customer, forward the customer's
    reply back to the broker on WhatsApp so nothing is missed."""
    if not broker_phone or not text:
        return
    from notifications.whatsapp_notifier import _send
    name = ((requirements.get("_profile") or {}).get("name")) or "A customer"
    ch = {"whatsapp": "WhatsApp", "telegram": "Telegram", "web": "the website"}.get(
        _channel_of(session_id), "chat")
    body = (f"*{name}* replied on {ch}:\n“{text[:600]}”\n\n"
            f"Reply *message {name}: …* to answer them, or handle it from the dashboard.")
    try:
        _send(broker_phone, body)
    except Exception as e:
        logger.debug(f"forward customer reply failed: {e}")


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


def _broker_message_customer(text: str, reply_context_id: str | None = None,
                             broker_phone: str | None = None):
    """Broker asks us to relay a message to a customer, on the customer's OWN channel
    (WhatsApp / Telegram / website). Phrases it as Riya speaking on the consultant's
    behalf, invites the customer to reply, and — if the broker committed to a time —
    schedules a reminder to the broker. Returns reply str, or None if it isn't a relay."""
    import json
    from agent.llm_client import complete
    try:
        js = complete([
            {"role": "system", "content": (
                "A real-estate broker wants Riya to relay a message to one of their customers. "
                "Return ONLY JSON with keys:\n"
                "\"target\": the customer's name or phone, or a pronoun ('him'/'her'/'them') if unnamed.\n"
                "\"message\": the update phrased as RIYA speaking TO the customer, referring to the "
                "broker in the third person as 'your consultant' — e.g. 'Your consultant will call you "
                "this evening tomorrow.' Do NOT write it in first person.\n"
                "\"followup_when\": if the broker committed to contacting the customer at a time, the "
                "time phrase exactly as said (e.g. 'this evening tomorrow', 'friday 5pm'); else \"\".\n"
                "If the broker is NOT asking to message a customer, return everything empty.")},
            {"role": "user", "content": text},
        ], temperature=0, max_tokens=180, json_mode=True)
        data = json.loads(js)
    except Exception:
        data = {}
    target = (data.get("target") or "").strip()
    message = (data.get("message") or "").strip()
    followup_when = (data.get("followup_when") or "").strip()
    if not message:
        return None

    session_id, phone, name = _resolve_customer(target, reply_context_id)
    if not session_id and not phone:
        return ("Who should I message? *Reply to* the customer's lead/visit message, "
                "or name them — e.g.\n*message Ravi: your consultant will call this evening* "
                "or *tell 9876543210 …*")

    first = (name or "").split()[0] if name else ""
    body = (f"Hi{(' ' + first) if first else ''} 👋 An update from Riya:\n\n"
            f"{message}\n\n"
            f"Need anything sooner or want to reschedule? Just reply here — I'm always around. 🌿")
    ok, channel = notify_customer(session_id, phone, body)
    who = name or phone or "them"
    if not ok:
        return (f"I couldn't reach *{who}* on {channel} right now"
                + (" — WhatsApp only allows messaging within 24 h of their last message."
                   if channel == "WhatsApp" else ".")
                + " You may need to contact them directly.")

    # Loop the broker in on whatever the customer replies next.
    if broker_phone:
        _mark_relay_open(session_id, broker_phone)

    # If the broker committed to a time, remind them then (so they don't forget).
    reminder_line = ""
    if followup_when and broker_phone:
        try:
            from agent.property_agent import _parse_visit_time
            from notifications.followups import add_followup
            dt, when_str = _parse_visit_time(followup_when)
            if dt:
                add_followup(dt, broker_phone, name or who, phone, session_id,
                             note=f"contact them ({when_str})")
                reminder_line = f"\n⏰ I'll remind you to follow up around *{when_str}*."
        except Exception as e:
            logger.debug(f"followup schedule skipped: {e}")

    return f"✅ Sent to *{who}* on {channel}:\n_{message}_{reminder_line}"


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


# ── Pipeline control from WhatsApp ───────────────────────────────────────────
# Natural language → pipeline status. Order matters (specific before general).
_STAGE_PATTERNS = [
    (r"site\s*visit|\bvisited\b|\bmet\b|seen (the|it|property)", "met", "Site Visited"),
    (r"visit\s*schedul|schedul|book(ed)?\s*(a\s*)?visit|for (a )?visit", "visit", "Visit Scheduled"),
    (r"negotiat", "negotiating", "Negotiating"),
    (r"\bwon\b|closed|deal done|sold|finali[sz]ed|booked the deal", "won", "Won"),
    (r"on\s*hold|\bhold\b|waiting|pause|paused|not now|back\s*burner|park (him|her|them|it)", "waiting", "On Hold"),
    (r"lost|not interested|\bdead\b|drop(ped)?", "lost", "Lost"),
    (r"contact(ed)?|called|reached out|spoke", "contacted", "Contacted"),
    (r"\bnew\b|fresh|reset", "new", "New"),
]
_STAGE_LABELS = {"new": "New", "contacted": "Contacted", "visit": "Visit Scheduled",
                 "met": "Site Visited", "negotiating": "Negotiating", "won": "Won",
                 "waiting": "On Hold", "lost": "Lost"}
# Warm nudges we send the customer when they reach an engagement stage.
_STAGE_MSG = {
    "met": ("Hi{f} 👋 Hope you liked the property you visited! If you have any questions "
            "or want to move ahead, just reply here — I'm happy to help. 🌿"),
    "negotiating": ("Hi{f} 👋 Great that you're interested! Our consultant is working out the "
                    "best possible price for you and will be in touch shortly. Reply here anytime. 🌿"),
}


def stage_change_nudge(session_id, phone, name, status, broker_phone=None):
    """Message the customer on their channel when moved to an engagement stage.
    Returns (ok, channel) or None."""
    if status not in _STAGE_MSG or not (session_id or phone):
        return None
    nm = name or ""
    first = nm.split()[0] if nm and nm.lower() != "the customer" else ""
    body = _STAGE_MSG[status].format(f=(" " + first) if first else "")
    ok, channel = notify_customer(session_id, phone, body)
    if ok and broker_phone:
        _mark_relay_open(session_id, broker_phone)
    return (ok, channel) if ok else None
_MOVE_VERB_RE = re.compile(
    r"\b(move|shift|put|mark|set|change|drag|reassign|update)\b", re.I)
_SEARCH_VERB_RE = re.compile(
    r"\b(show|list|search|find|who|which|give me|see|display|all)\b", re.I)


def _parse_stage(text: str):
    for pat, st, lbl in _STAGE_PATTERNS:
        if re.search(pat, text, re.I):
            return st, lbl
    return None, None


def _resolve_lead(text: str, reply_context_id: str | None = None):
    """Find the lead a move/query refers to → lead row (id,name,phone,status) or None."""
    from database.supabase_client import get_client
    c = get_client()
    m = re.search(r"\d{10}", re.sub(r"\D", " ", text))
    if m:
        rows = (c.table("leads").select("id,name,phone,status,session_id").ilike("phone", f"%{m.group(0)}%")
                .order("created_at", desc=True).limit(1).execute().data) or []
        if rows:
            return rows[0]
    # strip command + stage + filler words to isolate a name
    cleaned = re.sub(
        r"\b(move|shift|put|mark|set|change|drag|reassign|update|him|her|them|to|the|customer|"
        r"lead|on|as|into|status|stage|new|contacted|visit|visited|schedul\w*|site|met|seen|"
        r"negotiat\w*|won|closed|sold|hold|waiting|pause\w*|lost|not|interested|dead|drop\w*|"
        r"back|please|pls|now|over|kindly|just|again|this|that)\b",
        " ", text, flags=re.I)
    name = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]", " ", cleaned)).strip()
    if len(name) >= 2:
        # try the whole phrase, then individual tokens (longest first) — "Ravi back" → "Ravi"
        candidates = [name] + sorted({w for w in name.split() if len(w) >= 2}, key=len, reverse=True)
        for cand in candidates:
            rows = (c.table("leads").select("id,name,phone,status,session_id").ilike("name", f"%{cand}%")
                    .order("created_at", desc=True).limit(1).execute().data) or []
            if rows:
                return rows[0]
    if reply_context_id:
        ctx = context_customer(reply_context_id)
        if ctx and ctx[1]:
            rows = (c.table("leads").select("id,name,phone,status,session_id").ilike("phone", f"%{ctx[1]}%")
                    .limit(1).execute().data) or []
            if rows:
                return rows[0]
    return None


def _broker_move_lead(text: str, reply_context_id: str | None = None, broker_phone: str | None = None):
    """Broker moves a customer to a pipeline stage. Returns reply, or None if no stage."""
    st, lbl = _parse_stage(text)
    if not st:
        return None
    lead = _resolve_lead(text, reply_context_id)
    if not lead:
        return ("Which customer? *Reply to* their card/lead message, or name them — e.g.\n"
                "*move Ravi to negotiating* or *put 9876543210 on hold*.")
    from database.supabase_client import update_lead_status
    try:
        update_lead_status(lead["id"], st)
    except Exception as e:
        logger.error(f"broker move lead failed: {e}")
        return "I couldn't update that just now — please try again."
    nm = lead.get("name") or "the customer"

    # Stage-change auto-message on the engagement stages.
    extra = ""
    res = stage_change_nudge(lead.get("session_id"), lead.get("phone"), nm, st, broker_phone)
    if res:
        _ok, channel = res
        extra = f"\nMessaged {nm} on {channel} — I'll forward their reply."
    return f"Moved *{nm}* → *{lbl}*.{extra}"


def _broker_search_leads(text: str):
    """List leads, optionally filtered by a stage and/or a name/phone in the message."""
    from database.supabase_client import get_client
    c = get_client()
    st, lbl = _parse_stage(text)
    q = (c.table("leads").select("name,phone,status,preferred_area,budget_max,created_at")
         .order("created_at", desc=True).limit(20))
    if st:
        q = q.eq("status", st)
    # a phone or a name to narrow by?
    mph = re.search(r"\d{10}", re.sub(r"\D", " ", text))
    name = ""
    if mph:
        q = q.ilike("phone", f"%{mph.group(0)}%")
    else:
        cleaned = re.sub(
            r"\b(show|list|search|find|who'?s?|which|whom|give|gimme|me|see|display|all|any(one|body)?|"
            r"for|leads?|customers?|clients?|buyers?|people|person|pipeline|in|is|are|am|do|does|we|have|"
            r"the|my|our|a|an|status|stage|come|coming|came|today|now|still|currently|please|pls|kindly|"
            r"deciding|thinking|price|new|contacted|visit\w*|schedul\w*|site|met|negotiat\w*|won|closed|"
            r"sold|hold|waiting|pause\w*|lost|on|back|burner|look\s?up|lookup)\b",
            " ", text, flags=re.I)
        name = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]", " ", cleaned)).strip()
        # Only treat it as a name if it's short & name-like (1-2 words) — otherwise it's
        # just leftover phrasing and we should rely on the stage filter (or list all).
        if 2 <= len(name) <= 40 and 1 <= len(name.split()) <= 2:
            q = q.ilike("name", f"%{name}%")
        else:
            name = ""
    try:
        rows = q.execute().data or []
    except Exception as e:
        logger.warning(f"broker search leads failed: {e}")
        return "Couldn't fetch leads right now — try again in a moment."
    title = name or (mph.group(0) if mph else None) or lbl or "All leads"
    if not rows:
        return f"No leads found for *{title}*."
    head = f"*{title}* ({len(rows)})"
    lines = [head]
    for r in rows[:15]:
        area = r.get("preferred_area") or ""
        b = r.get("budget_max")
        bs = f" · ₹{b/1e7:.1f}Cr" if b else ""
        stlbl = _STAGE_LABELS.get(r.get("status"), r.get("status") or "")
        extra = " · ".join(x for x in [stlbl, area] if x)
        lines.append(f"• *{r.get('name') or 'Unknown'}* ({r.get('phone') or ''})"
                     + (f" — {extra}" if extra else "") + bs)
    return "\n".join(lines)


# ── One-shot "ask the customer & auto-reschedule if they say yes" ────────────
_ASK_RE = re.compile(r"\b(ask|check with|check if|find out if|see if|confirm with)\b", re.I)
_TIME_SIGNAL = re.compile(
    r"\d|morning|evening|afternoon|tonight|tomorrow|today|noon|night|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|am|pm|o'?clock", re.I)


def _upcoming_meeting_for_lead(lead_id: str):
    """The lead's next non-cancelled meeting (id) if any."""
    from database.supabase_client import get_client
    from datetime import datetime, timezone
    try:
        now = datetime.now(timezone.utc).isoformat()
        rows = (get_client().table("meetings").select("id,scheduled_at,property_id")
                .eq("lead_id", lead_id).neq("status", "cancelled")
                .gte("scheduled_at", now).order("scheduled_at").limit(1).execute().data) or []
        return rows[0] if rows else None
    except Exception:
        return None


def _broker_ask_customer_time(text: str, reply_context_id: str | None, broker_phone: str | None):
    """Broker: 'ask Ravi if he's free Saturday 5pm'. We message the customer, and when
    they reply YES we auto-reschedule. Returns a reply, or None if we can't act."""
    from agent.property_agent import _parse_visit_time
    lead = _resolve_lead(text, reply_context_id)
    if not lead:
        return ("Who should I ask? Name them or reply to their card — e.g.\n"
                "*ask Ravi if he's free Saturday 5pm*.")
    dt, when = _parse_visit_time(text)
    if not dt:
        return f"What time should I check with *{lead.get('name') or 'them'}*? e.g. *ask them if free Saturday 5pm*."

    mtg = _upcoming_meeting_for_lead(lead["id"])
    meeting_id = (mtg or {}).get("id")
    session_id = lead.get("session_id")
    phone = lead.get("phone")
    nm = lead.get("name") or "there"
    first = nm.split()[0] if nm != "there" else ""

    ask_msg = (f"Hi{(' ' + first) if first else ''} 👋 Would *{when}* work for your property visit? "
               f"Reply *YES* to confirm, or just tell me a better day/time. 🌿")
    ok, channel = notify_customer(session_id, phone, ask_msg)
    if not ok:
        return f"I couldn't reach *{nm}* on {channel} right now — you may need to contact them directly."

    # Remember the pending ask on the customer's session so their reply auto-reschedules.
    from database.supabase_client import get_session, save_session
    try:
        s = get_session(session_id)
        req = s.get("requirements") or {}
        req["_pending_reschedule_ask"] = {
            "proposed_when": when,
            "proposed_dt": dt.isoformat(),
            "meeting_id": meeting_id,
            "lead_id": lead["id"],
            "broker_phone": broker_phone,
            "name": nm,
        }
        req["_relay_broker"] = broker_phone   # loop the broker in on whatever they say
        save_session(session_id, s.get("messages") or [], req, s.get("stage") or "scheduling")
    except Exception as e:
        logger.debug(f"pending ask save failed: {e}")

    return (f"Asked *{nm}* on {channel} if *{when}* works. "
            f"I'll auto-reschedule and confirm both of you the moment they agree.")


def handle_customer_reschedule_reply(conv, text: str, ask: dict):
    """Customer replied to a broker's 'are you free at X?' ask. Interpret and act.
    Returns a customer-facing reply (str) when handled, or None to fall through."""
    from database.supabase_client import update_meeting
    from notifications.whatsapp_notifier import _send
    from agent.property_agent import _parse_visit_time

    broker_phone = ask.get("broker_phone")
    meeting_id = ask.get("meeting_id")
    nm = ask.get("name") or "The customer"
    proposed_when = ask.get("proposed_when")
    proposed_dt = ask.get("proposed_dt")

    decision = _broker_decision(text)          # reuse YES/NO detection
    dt2, when2 = _parse_visit_time(text)
    gave_time = dt2 is not None and _TIME_SIGNAL.search(text or "")

    # Decline → tell the broker, stop here.
    if decision == "no" and not gave_time:
        conv.requirements.pop("_pending_reschedule_ask", None)
        if broker_phone:
            _send(broker_phone, f"*{nm}* can't make *{proposed_when}*: “{text[:200]}”. "
                                f"Reply *ask {nm} if free <another time>* to try again.")
        return ("No problem — I'll let our consultant know and we'll find a time that suits you. "
                "Feel free to suggest one here. 🌿")

    # Confirmed (yes) or gave a concrete alternative time → reschedule.
    use_dt, use_when = (dt2, when2) if gave_time else (None, proposed_when)
    if not gave_time and decision != "yes":
        return None  # ambiguous — let the normal agent handle it, keep the ask pending

    from datetime import datetime
    final_dt = None
    if use_dt is not None:
        final_dt = use_dt
    elif proposed_dt:
        try:
            final_dt = datetime.fromisoformat(proposed_dt)
        except Exception:
            final_dt = None

    conv.requirements.pop("_pending_reschedule_ask", None)
    if meeting_id and final_dt:
        try:
            update_meeting(meeting_id, {
                "scheduled_at": final_dt.isoformat(),
                "status": "confirmed",
                "notes": f"Customer confirmed via Riya for {use_when}",
            })
        except Exception as e:
            logger.error(f"auto-reschedule update_meeting failed: {e}")
    if broker_phone:
        _send(broker_phone, f"*{nm}* confirmed — visit set for *{use_when}*. "
                            f"It's on the calendar; both of you are notified.")
    return (f"Perfect — your visit is confirmed for *{use_when}*. "
            f"Our consultant will see you then! 🌿")


def _classify_broker_intent(text: str) -> str:
    """LLM intent router — catches natural phrasings the keyword rules miss."""
    from agent.llm_client import complete
    import json
    try:
        js = complete([
            {"role": "system", "content": (
                "Classify a real-estate broker's WhatsApp message into ONE intent. Return ONLY "
                "JSON {\"intent\":\"...\"}. Intents:\n"
                "meetings — their upcoming visits/appointments\n"
                "stats — counts of leads/listings/visits\n"
                "search — show/find/list leads or customers (by stage or name)\n"
                "move — change a customer's pipeline stage (put on hold / back burner, mark won/lost, "
                "negotiating, contacted, site visited…)\n"
                "ask — ask a customer whether a time works, to reschedule a visit\n"
                "message — send/relay a message to a customer\n"
                "add_property — add a new listing\n"
                "chat — anything else / a general question\n"
                "Pick the single best fit.")},
            {"role": "user", "content": text},
        ], temperature=0, max_tokens=24, json_mode=True)
        return (json.loads(js).get("intent") or "chat").strip().lower()
    except Exception:
        return "chat"


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
    # Move a lead across the pipeline: "move Ravi to negotiating", "put X on hold"
    if _MOVE_VERB_RE.search(tl) and _parse_stage(tl)[0]:
        moved = _broker_move_lead(t, reply_context_id, broker_phone)
        if moved is not None:
            return moved
    # Search / list the pipeline: "show new leads", "who is on negotiations", "find Ravi"
    # (no trailing \b on the nouns — so plurals like customerS / negotiationS match)
    if re.search(r"\b(find|search|look\s?up|lookup)\b", tl) or (
            _SEARCH_VERB_RE.search(tl) and re.search(
                r"\b(lead|customer|client|pipeline|list|new|contacted|negotiat|won|hold|waiting|visit|lost)", tl)):
        return _broker_search_leads(t)
    # Ask a customer to confirm a time → auto-reschedule when they say yes.
    if _ASK_RE.search(tl) and _TIME_SIGNAL.search(tl):
        asked = _broker_ask_customer_time(t, reply_context_id, broker_phone)
        if asked is not None:
            return asked
    if _STATS_RE.search(tl) or (_HOWMANY_RE.search(tl) and _BIZ_NOUN_RE.search(tl)):
        return _broker_stats()
    if _HELP_RE.search(tl):
        return _broker_help_menu()
    if _ADD_PROP_RE.search(tl):
        return _broker_add_property_from_text(t, broker_phone)
    # Relay a message to a customer. Treat it as a relay if it's clearly a message verb
    # (not "tell me …"), OR the broker is replying to a customer's lead/visit notification.
    if reply_context_id or (_MSG_VERB_RE.search(tl) and not _ASK_SELF_RE.search(tl)):
        relayed = _broker_message_customer(t, reply_context_id, broker_phone)
        if relayed is not None:
            return relayed

    # Keyword rules didn't catch it — ask the LLM what the broker wants, then act.
    intent = _classify_broker_intent(t)
    if intent == "meetings":
        return _broker_meetings_summary()
    if intent == "stats":
        return _broker_stats()
    if intent == "search":
        return _broker_search_leads(t)
    if intent == "move":
        r = _broker_move_lead(t, reply_context_id, broker_phone)
        if r is not None:
            return r
    if intent == "ask":
        r = _broker_ask_customer_time(t, reply_context_id, broker_phone)
        if r is not None:
            return r
    if intent == "message":
        r = _broker_message_customer(t, reply_context_id, broker_phone)
        if r is not None:
            return r
    if intent == "add_property":
        return _broker_add_property_from_text(t, broker_phone)
    # Genuinely conversational — reply like a real assistant.
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

        # WhatsApp the broker confirmation (calendar event is auto-added below +
        # emailed as an invite — no need to dump a raw link into the chat).
        broker_gcal = _gcal_link(proposed_dt, "Property visit", f"Visit with {buyer_name}", "Lucknow")
        _send(broker_phone, f"Confirmed — visit with *{buyer_name}* booked for *{proposed_when}*. "
                            f"The buyer's been notified and it's on your calendar.")
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

    _send(broker_phone,
          f"Done — rescheduled to *{proposed_when}*. "
          f"{buyer_name} has been notified.")
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
