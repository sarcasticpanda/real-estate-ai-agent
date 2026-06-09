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

from database.supabase_client import save_lead, get_broker_for_area

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"(?:\+91[-\s]?)?[6-9]\d{9}")
# Single-word Indian names are common (Riya, Raj, Priya, Amit etc.)
NAME_MIN_LEN = 2  # minimum character length for a name word


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
        saved = save_lead(lead_data)
        logger.info(f"Lead saved: {saved.get('id')} — {name} ({phone})")
        return saved
    except Exception as e:
        logger.error(f"Failed to save lead: {e}")
        return None


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
