"""
Tests all 3 notification channels: Email, WhatsApp broker alert, WhatsApp buyer confirm.

Usage:
    python scripts/test_all_notifications.py

Uses the broker phone/email from .env and DB.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

fake_lead = {
    "id": "test-lead-001",
    "name": "Rahul Sharma",
    "phone": "9876543210",
    "preferred_bhk": 3,
    "preferred_area": "Gomti Nagar",
}
fake_requirements = {
    "area": "Gomti Nagar",
    "bhk": 3,
    "max_budget_cr": 1.5,
    "email": None,  # buyer email not available in test
}

print("=" * 50)
print("Real Estate Bot — Notification Test")
print("=" * 50)

# ── Email ────────────────────────────────────────────────────────────────────
print("\n[1] Email — broker alert + buyer confirmation")
from notifications.email_notifier import notify_broker_email, notify_buyer_email

import os
broker_email = os.environ.get("GMAIL_ADDRESS")
if broker_email:
    ok_broker = notify_broker_email(fake_lead, fake_requirements, broker_email)
    print(f"  Broker email to {broker_email}: {'✅ sent' if ok_broker else '❌ failed'}")
    ok_buyer = notify_buyer_email(broker_email, "Rahul Sharma", "Saurabh Kumar", "9936659513", "Gomti Nagar", 3)
    print(f"  Buyer email to {broker_email}: {'✅ sent' if ok_buyer else '❌ failed'}")
else:
    print("  ❌ GMAIL_ADDRESS not set in .env")

# ── WhatsApp ─────────────────────────────────────────────────────────────────
print("\n[2] WhatsApp — broker alert + buyer confirmation")
from notifications.whatsapp_notifier import notify_broker_whatsapp, notify_buyer_whatsapp

BROKER_PHONE = "9936659513"

ok_wa_broker = notify_broker_whatsapp(fake_lead, fake_requirements, BROKER_PHONE)
print(f"  Broker WhatsApp to {BROKER_PHONE}: {'✅ sent' if ok_wa_broker else '❌ failed (check token + test recipient)'}")

ok_wa_buyer = notify_buyer_whatsapp(BROKER_PHONE, "Rahul Sharma", "Saurabh Kumar", BROKER_PHONE, "Gomti Nagar", 3)
print(f"  Buyer WhatsApp to {BROKER_PHONE}: {'✅ sent' if ok_wa_buyer else '❌ failed'}")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 50)
all_ok = ok_broker and ok_wa_broker
print("✅ All channels working!" if all_ok else "⚠️  Some notifications failed — see details above")
print("\nNOTE: WhatsApp in dev mode only works for registered test recipients.")
print("Add your number at: developers.facebook.com → RealEstateBot → WhatsApp → API Setup → Manage")
