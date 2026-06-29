"""
WhatsApp Cloud API notifications (free — 1000 conversations/month).

Sends:
  - Broker alert when a new lead comes in
  - Buyer confirmation after lead is captured

IMPORTANT (Development mode limits):
  - Can only message phone numbers added as test recipients in Meta dashboard
  - To add a number: developers.facebook.com → RealEstateBot → WhatsApp → API Setup → "To" dropdown → Manage
  - Token rotates every ~24h in dev mode. Replace WHATSAPP_ACCESS_TOKEN in .env when it expires.
  - For permanent token: Meta Business Suite → System Users → Generate Token
"""

import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GRAPH_API_URL = "https://graph.facebook.com/v25.0"
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
ACCESS_TOKEN    = os.environ.get("WHATSAPP_ACCESS_TOKEN")


def _send(to_phone: str, message: str) -> bool:
    """
    Send a plain text WhatsApp message.
    to_phone: Indian number in format '919876543210' (91 + 10 digits, no +)
    """
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        logger.warning("WhatsApp not configured — WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN missing")
        return False

    # Normalise number: strip +, spaces, dashes; ensure 91 prefix
    phone = to_phone.replace("+", "").replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = phone[1:]
    if not phone.startswith("91") and len(phone) == 10:
        phone = "91" + phone

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": message},
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            f"{GRAPH_API_URL}/{PHONE_NUMBER_ID}/messages",
            json=payload,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"WhatsApp sent to {phone}")
        return True
    except requests.HTTPError as e:
        logger.error(f"WhatsApp send failed ({resp.status_code}): {resp.text}")
        return False
    except Exception as e:
        logger.error(f"WhatsApp send error: {e}")
        return False


def send_image(to_phone: str, image_url: str, caption: str = "") -> bool:
    """
    Send an image message (property photo) by public URL with an optional caption.
    Best-effort — logs and returns False on failure, never raises.
    """
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN or not image_url:
        return False

    phone = to_phone.replace("+", "").replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = phone[1:]
    if not phone.startswith("91") and len(phone) == 10:
        phone = "91" + phone

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "image",
        "image": {"link": image_url},
    }
    if caption:
        payload["image"]["caption"] = caption[:1024]

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            f"{GRAPH_API_URL}/{PHONE_NUMBER_ID}/messages",
            json=payload,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.HTTPError:
        logger.warning(f"WhatsApp image send failed ({resp.status_code}): {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"WhatsApp image send error: {e}")
        return False


def mark_read(message_id: str, typing: bool = True) -> bool:
    """
    Mark an inbound message as read (shows blue double-ticks to the sender) and,
    optionally, show a "typing…" indicator while we generate the reply.

    The typing indicator auto-dismisses when we send our next message, or after
    ~25s. Safe to call best-effort — failures are logged, never raised.
    """
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN or not message_id:
        return False

    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    if typing:
        payload["typing_indicator"] = {"type": "text"}

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            f"{GRAPH_API_URL}/{PHONE_NUMBER_ID}/messages",
            json=payload,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"WhatsApp mark_read failed: {e}")
        return False


def notify_broker_whatsapp(lead: dict, requirements: dict, broker_phone: str | None) -> bool:
    """Send new lead alert to broker via WhatsApp."""
    if not broker_phone:
        logger.warning("No broker phone — WhatsApp broker alert skipped")
        return False

    area    = requirements.get("area") or requirements.get("city", "Lucknow")
    bhk     = requirements.get("bhk", "")
    budget  = requirements.get("max_budget_cr")
    budget_str = f"₹{budget} crore" if budget else "not specified"
    name    = lead.get("name", "Unknown")
    phone   = lead.get("phone", "N/A")

    message = (
        f"🏠 *New Lead — Real Estate Bot*\n\n"
        f"👤 Customer: {name}\n"
        f"📞 Phone: {phone}\n"
        f"🏘️ Looking for: {bhk} BHK in {area}\n"
        f"💰 Budget: {budget_str}\n\n"
        f"⚡ Please call within 2 hours!\n"
        f"Lead ID: {lead.get('id', '')}"
    )
    return _send(broker_phone, message)


def notify_buyer_whatsapp(buyer_phone: str, buyer_name: str, broker_name: str, broker_phone: str, area: str, bhk) -> bool:
    """Send confirmation to buyer via WhatsApp after lead is captured."""
    message = (
        f"Namaste {buyer_name}! 🙏\n\n"
        f"Your interest in a {bhk} BHK property in {area} has been noted.\n\n"
        f"Our broker *{broker_name}* will call you shortly.\n"
        f"📞 Broker: {broker_phone}\n\n"
        f"Thank you for using our service! — Riya 🏠"
    )
    return _send(buyer_phone, message)


# ── Quick test (run directly) ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    test_phone = sys.argv[1] if len(sys.argv) > 1 else input("Enter test phone (10 digits): ")
    ok = _send(test_phone, "Hello from Real Estate Bot! 🏠 WhatsApp integration is working.")
    print("Sent ✅" if ok else "Failed ❌ — check logs above")
