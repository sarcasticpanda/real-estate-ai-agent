"""
Lead capture and broker notification.
When a user expresses clear interest, collect their name + phone
and save a qualified lead to Supabase.
"""

import re
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.supabase_client import (
    save_lead, get_broker_for_area, get_user_profile,
    get_recent_lead_by_phone, update_lead,
)
from notifications.email_notifier import notify_broker_email, notify_buyer_email
from notifications.whatsapp_notifier import notify_broker_whatsapp, notify_buyer_whatsapp

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"(?:\+91[-\s]?)?[6-9]\d{9}")
# Single-word Indian names are common (Riya, Raj, Priya, Amit etc.)
NAME_MIN_LEN = 2  # minimum character length for a name word


def is_fake_phone(phone: str | None) -> bool:
    """True for obviously bogus numbers: all-same digit, ≤2 distinct digits, or a
    straight ascending/descending run (1234567890 / 9876543210)."""
    if not phone:
        return True
    d = re.sub(r"\D", "", phone)[-10:]
    if len(d) < 10:
        return True
    if len(set(d)) <= 2:
        return True
    start = int(d[0])
    asc = "".join(str((start + i) % 10) for i in range(10))
    desc = "".join(str((start - i) % 10) for i in range(10))
    return d in (asc, desc)


def extract_name_and_phone(text: str) -> tuple[str | None, str | None]:
    """Try to extract name and phone number from a freeform user message."""
    phone_match = PHONE_RE.search(text)
    phone = phone_match.group(0).replace(" ", "").replace("-", "") if phone_match else None

    # Remove phone, strip filler phrases, collect name candidates
    text_without_phone = PHONE_RE.sub("", text).strip()
    text_lower = text_without_phone.lower()
    for phrase in ["my name is", "name is", "i am", "i'm", "mera naam hai",
                   "naam hai", "call me", "number is", "phone is", "yahan hai"]:
        text_lower = text_lower.replace(phrase, "")

    # Rebuild with original casing where possible
    words = [w.strip(",.;:!?") for w in text_without_phone.split()]
    name_words = [w for w in words if w.isalpha() and len(w) >= NAME_MIN_LEN]

    # 1 word is fine (single-name users are common), take up to 3
    name = " ".join(name_words[:3]).title() if name_words else None

    return name, phone


def create_lead(
    session_id: str,
    requirements: dict,
    name: str | None,
    phone: str | None,
    property_id: str | None = None,
) -> dict | None:
    """Save a qualified lead to Supabase and return the saved record."""
    # Pre-fill name/phone from stored user profile if not provided by current message
    profile = get_user_profile(session_id)
    if not name and profile.get("name"):
        name = profile["name"]
    if not phone and profile.get("phone"):
        phone = profile["phone"]
    # Pull email from profile for buyer confirmation email
    if not requirements.get("email") and profile.get("email"):
        requirements = {**requirements, "email": profile["email"]}

    area = requirements.get("area") or requirements.get("city", "Lucknow")
    broker = get_broker_for_area(area)

    crore = 1_00_00_000
    max_budget_cr = requirements.get("max_budget_cr")
    min_budget_cr = requirements.get("min_budget_cr")

    lead_data = {
        "session_id": session_id,
        "name": name,
        "phone": phone,
        "budget_max": int(max_budget_cr * crore) if max_budget_cr else None,
        "budget_min": int(min_budget_cr * crore) if min_budget_cr else None,
        "preferred_bhk": requirements.get("bhk"),
        "preferred_city": requirements.get("city", "Lucknow"),
        "preferred_area": requirements.get("area"),
        "interested_property_id": property_id,
        "broker_id": broker["id"] if broker else None,
        "status": "new",
    }

    try:
        # Dedup: if this phone already led recently, refresh that lead instead of
        # inserting a duplicate (broker shouldn't get the same person twice).
        existing = None
        try:
            existing = get_recent_lead_by_phone(phone) if phone else None
        except Exception as e:
            logger.warning(f"dedup lookup failed (continuing): {e}")

        if existing:
            refresh = {k: v for k, v in lead_data.items() if v is not None}
            refresh["status"] = existing.get("status") or "new"  # keep broker's progress
            saved = update_lead(existing["id"], refresh) or existing
            logger.info(f"Lead deduped → updated existing {existing['id']} — {name} ({phone})")
        else:
            saved = save_lead(lead_data)
            logger.info(f"Lead saved: {saved.get('id')} — {name} ({phone})")

        # Fire-and-forget notifications — failures don't block the lead save
        _send_lead_notifications(saved, requirements, broker, name, phone)

        return saved
    except Exception as e:
        logger.error(f"Failed to save lead: {e}")
        return None


def _send_lead_notifications(lead: dict, requirements: dict, broker: dict | None, name: str, phone: str) -> None:
    """Send broker alert + buyer confirmation via email and WhatsApp."""
    broker_name  = broker.get("name", "our broker") if broker else "our broker"
    broker_phone = broker.get("phone", "") if broker else ""
    area = requirements.get("area", "Lucknow")
    bhk  = requirements.get("bhk", "")

    # ── Email to broker ───────────────────────────────────────────────────────
    try:
        notify_broker_email(lead, requirements, broker.get("email") if broker else None)
    except Exception as e:
        logger.warning(f"Broker email failed: {e}")

    # ── WhatsApp to broker ────────────────────────────────────────────────────
    try:
        notify_broker_whatsapp(lead, requirements, broker_phone)
    except Exception as e:
        logger.warning(f"Broker WhatsApp failed: {e}")

    # ── Email + WhatsApp to buyer (only if we have their contact) ─────────────
    buyer_email = requirements.get("email")
    if buyer_email:
        try:
            notify_buyer_email(buyer_email, name, broker_name, broker_phone, area, bhk)
        except Exception as e:
            logger.warning(f"Buyer email failed: {e}")

    if phone:
        try:
            notify_buyer_whatsapp(phone, name, broker_name, broker_phone, area, bhk)
        except Exception as e:
            logger.warning(f"Buyer WhatsApp failed: {e}")


def notify_broker_via_n8n(lead: dict, requirements: dict, n8n_webhook_url: str | None = None) -> bool:
    """
    Trigger n8n lead notification webhook.
    n8n then sends Telegram/email to the broker.
    """
    import os
    import requests

    webhook_url = n8n_webhook_url or os.environ.get("N8N_LEAD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("N8N_LEAD_WEBHOOK_URL not set — broker not notified via n8n")
        return False

    payload = {
        "lead_id": lead.get("id"),
        "customer_name": lead.get("name", "Unknown"),
        "customer_phone": lead.get("phone", "N/A"),
        "preferred_area": requirements.get("area", ""),
        "preferred_bhk": requirements.get("bhk", ""),
        "max_budget_cr": requirements.get("max_budget_cr", ""),
        "interested_property": lead.get("interested_property_id", ""),
        "broker_id": lead.get("broker_id", ""),
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(f"n8n notified successfully for lead {lead.get('id')}")
        return True
    except Exception as e:
        logger.error(f"n8n webhook failed: {e}")
        return False
