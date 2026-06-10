"""Full system state check — run before any deployment."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv; load_dotenv()
import os

print("\n=== ENV VARS ===")
keys = {
    "SUPABASE_URL": "required",
    "SUPABASE_KEY": "required",
    "GROQ_API_KEY": "required",
    "TELEGRAM_BOT_TOKEN": "required",
    "GMAIL_ADDRESS": "required",
    "GMAIL_APP_PASSWORD": "required",
    "WHATSAPP_PHONE_NUMBER_ID": "required",
    "WHATSAPP_ACCESS_TOKEN": "required",
    "WHATSAPP_VERIFY_TOKEN": "required",
    "WHATSAPP_BUSINESS_ACCOUNT_ID": "optional",
    "N8N_LEAD_WEBHOOK_URL": "optional",
}
all_ok = True
for k, req in keys.items():
    v = os.environ.get(k, "")
    status = "[OK]    " if v else ("[MISSING]" if req == "required" else "[SKIP]  ")
    if not v and req == "required":
        all_ok = False
    display = (v[:30] + "...") if len(v) > 30 else v
    print("  {} {} {}".format(status, k, "= " + display if v else ""))

print("\n=== DATABASE STATE ===")
try:
    from database.supabase_client import get_client
    c = get_client()

    brokers = c.table("brokers").select("id,name,phone,email,areas").execute()
    print("Brokers:     {} record(s)".format(len(brokers.data)))
    for b in brokers.data:
        areas = b.get("areas") or []
        print("  - {} | phone={} | email={} | {} areas covered".format(
            b.get("name"), b.get("phone"), b.get("email"), len(areas)))

    total = c.table("properties").select("id", count="exact").execute()
    print("Properties:  {} total".format(total.count))

    sample = c.table("properties").select("id,data").limit(10).execute()
    has_img = sum(1 for p in sample.data if (p.get("data") or {}).get("images"))
    print("  Images:    {}/10 sampled have images".format(has_img))

    leads = c.table("leads").select("id,name,status").order("created_at", desc=True).limit(3).execute()
    print("Leads:       {} recent".format(len(leads.data)))
    for l in leads.data:
        print("  - {} [{}]".format(l.get("name"), l.get("status")))

    sessions = c.table("sessions").select("session_id", count="exact").execute()
    print("Sessions:    {} total".format(sessions.count))
except Exception as e:
    print("  DB ERROR: {}".format(e))
    all_ok = False

print("\n=== MODULE IMPORTS ===")
modules = [
    ("agent.property_agent",         "process_message"),
    ("agent.intent_extractor",       "extract_intent"),
    ("agent.lead_collector",         "create_lead"),
    ("rag.retriever",                "retrieve"),
    ("notifications.email_notifier", "notify_broker_email"),
    ("notifications.whatsapp_notifier", "notify_broker_whatsapp"),
    ("database.supabase_client",     "get_user_profile"),
]
for mod, fn in modules:
    try:
        m = __import__(mod, fromlist=[fn])
        getattr(m, fn)
        print("  [OK]    {}".format(mod))
    except Exception as e:
        print("  [FAIL]  {}: {}".format(mod, e))
        all_ok = False

print("\n=== RESULT ===")
print("  " + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED — see above"))
