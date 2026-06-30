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


def _normalize_phone(to_phone: str) -> str:
    phone = to_phone.replace("+", "").replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = phone[1:]
    if not phone.startswith("91") and len(phone) == 10:
        phone = "91" + phone
    return phone


def _upload_media(image_url: str) -> str | None:
    """
    Download an image from a public URL and upload it to WhatsApp's media endpoint,
    returning a media_id. WhatsApp often refuses third-party 'link' images (Unsplash,
    redirects, no file extension), so uploading the bytes ourselves is far more reliable.
    Returns None on any failure.
    """
    try:
        img = requests.get(image_url, timeout=15)
        img.raise_for_status()
        content_type = img.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if content_type not in ("image/jpeg", "image/png"):
            content_type = "image/jpeg"
        ext = "png" if content_type == "image/png" else "jpg"
        files = {
            "file": (f"property.{ext}", img.content, content_type),
        }
        data = {"messaging_product": "whatsapp", "type": content_type}
        resp = requests.post(
            f"{GRAPH_API_URL}/{PHONE_NUMBER_ID}/media",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            files=files,
            data=data,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("id")
    except Exception as e:
        logger.warning(f"WhatsApp media upload failed: {e}")
        return None


def send_image(to_phone: str, image_url: str, caption: str = "") -> bool:
    """
    Send a property photo with an optional caption. Tries the reliable media-upload path
    first (download bytes -> upload -> send by media_id); falls back to the raw link.
    Best-effort — logs and returns False on failure, never raises.
    """
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN or not image_url:
        return False

    phone = _normalize_phone(to_phone)
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

    image_obj: dict
    media_id = _upload_media(image_url)
    if media_id:
        image_obj = {"id": media_id}
    else:
        image_obj = {"link": image_url}  # fallback
    if caption:
        image_obj["caption"] = caption[:1024]

    payload = {"messaging_product": "whatsapp", "to": phone, "type": "image", "image": image_obj}
    try:
        resp = requests.post(
            f"{GRAPH_API_URL}/{PHONE_NUMBER_ID}/messages",
            json=payload,
            headers=headers,
            timeout=20,
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
