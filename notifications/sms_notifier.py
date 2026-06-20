"""
SMS notifications via Fast2SMS (India) — free tier: ~500 credits on signup.
Falls back silently if FAST2SMS_API_KEY is not set.

Sign up at https://www.fast2sms.com → Dashboard → Dev API → copy API key
Set FAST2SMS_API_KEY in .env and Railway variables.

Usage:
    send_sms("9876543210", "Your visit is confirmed for Saturday 5pm.")
    send_visit_sms_buyer(phone, name, when)
    send_visit_sms_broker(phone, buyer_name, buyer_phone, when)
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

FAST2SMS_URL = "https://www.fast2sms.com/dev/bulkV2"
API_KEY = os.environ.get("FAST2SMS_API_KEY", "")


def send_sms(phone: str, message: str) -> bool:
    """
    Send a plain-text SMS to an Indian mobile number.
    phone: 10-digit or 91-prefixed Indian number.
    Returns True if sent, False otherwise (never raises).
    """
    if not API_KEY:
        logger.debug("FAST2SMS_API_KEY not set — SMS skipped")
        return False

    # Normalize: strip +91, spaces, dashes → 10 digits
    num = phone.replace("+", "").replace(" ", "").replace("-", "")
    if num.startswith("91") and len(num) == 12:
        num = num[2:]
    if len(num) != 10 or not num.isdigit():
        logger.warning(f"Invalid phone for SMS: {phone}")
        return False

    try:
        resp = requests.get(
            FAST2SMS_URL,
            headers={"authorization": API_KEY, "Content-Type": "application/json"},
            params={
                "variables_values": message,
                "route": "q",          # quick/transactional route
                "numbers": num,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("return") is True:
            logger.info(f"SMS sent to {num}")
            return True
        logger.warning(f"Fast2SMS returned error: {data}")
        return False
    except Exception as e:
        logger.error(f"SMS send failed: {e}")
        return False


def send_visit_sms_buyer(phone: str, name: str, when: str, area: str = "Lucknow") -> bool:
    """Tell the buyer their visit is confirmed."""
    msg = f"Hi {name}, your property visit in {area} is confirmed for {when}. - Riya AI"
    return send_sms(phone, msg)


def send_visit_sms_broker(phone: str, buyer_name: str, buyer_phone: str, when: str) -> bool:
    """Tell the broker about a new confirmed visit."""
    msg = f"New visit: {buyer_name} ({buyer_phone}) confirmed for {when}. - Riya AI"
    return send_sms(phone, msg)


def send_reschedule_sms_buyer(phone: str, name: str, new_when: str) -> bool:
    """Tell the buyer their visit was rescheduled."""
    msg = f"Hi {name}, your visit has been rescheduled to {new_when}. - Riya AI"
    return send_sms(phone, msg)


def send_lead_sms_broker(phone: str, buyer_name: str, buyer_phone: str, area: str, budget: str) -> bool:
    """Alert broker about a new lead."""
    msg = f"New lead: {buyer_name} ({buyer_phone}) looking in {area}, budget {budget}. - Riya AI"
    return send_sms(phone, msg)
