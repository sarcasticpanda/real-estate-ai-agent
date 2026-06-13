"""
Quick WhatsApp integration test.
Sends a test message to the number you pass as argument.

Usage:
    python scripts/test_whatsapp.py 9876543210
    python scripts/test_whatsapp.py 919876543210   # with country code

IMPORTANT: In Meta developer mode (even with permanent token), you can only
send to phone numbers added as test recipients.
To add your number:
  developers.facebook.com → RealEstateBot → WhatsApp → API Setup
  → "To" field dropdown → Manage phone numbers → Add number → verify OTP
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from notifications.whatsapp_notifier import _send, notify_broker_whatsapp, notify_buyer_whatsapp

phone = sys.argv[1] if len(sys.argv) > 1 else input("Enter test phone number (e.g. 9936659513): ").strip()

print(f"\n--- Test 1: Plain message to {phone} ---")
ok1 = _send(phone, "Hello from Real Estate Bot! 🏠 WhatsApp is working. — Riya")
print("✅ Sent!" if ok1 else "❌ Failed — check WHATSAPP_ACCESS_TOKEN in .env and that this number is a test recipient")

print(f"\n--- Test 2: Broker lead alert ---")
fake_lead = {"id": "test-lead-001", "name": "Rahul Sharma", "phone": "9876543210"}
fake_requirements = {"area": "Gomti Nagar", "bhk": 3, "max_budget_cr": 1.5}
ok2 = notify_broker_whatsapp(fake_lead, fake_requirements, phone)
print("✅ Broker alert sent!" if ok2 else "❌ Failed")

print(f"\n--- Test 3: Buyer confirmation ---")
ok3 = notify_buyer_whatsapp(phone, "Rahul Sharma", "Saurabh Kumar", "9936659513", "Gomti Nagar", 3)
print("✅ Buyer confirmation sent!" if ok3 else "❌ Failed")
