"""Update broker record to cover all Lucknow areas and set email."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv; load_dotenv()
from database.supabase_client import get_client

ALL_LUCKNOW_AREAS = [
    "Gomti Nagar", "Gomti Nagar Extension", "Gomtinagar", "Gomtinagar Extension",
    "Aliganj", "Indira Nagar", "Indiranagar", "Hazratganj", "Ashiana",
    "Alambagh", "Chowk", "Aminabad", "Mahanagar", "Raj Bhavan Road",
    "Thakurganj", "Kapoorthala", "Vikas Nagar", "Jankipuram",
    "Kursi Road", "Faizabad Road", "Sultanpur Road", "Rae Bareli Road",
    "Chinhat", "Sarojini Nagar", "Transport Nagar", "Vrindavan Yojna",
    "Sushant Golf City", "Kalyanpur", "Lucknow", "Nirala Nagar",
    "Butler Colony", "Eldeco", "Sector H",
]

c = get_client()
brokers = c.table("brokers").select("*").execute()

if brokers.data:
    bid = brokers.data[0]["id"]
    c.table("brokers").update({
        "areas": ALL_LUCKNOW_AREAS,
        "email": "temp2saubhagya@gmail.com",
        "is_active": True,
    }).eq("id", bid).execute()
    print("Broker '{}' updated — covers {} areas".format(
        brokers.data[0].get("name"), len(ALL_LUCKNOW_AREAS)))
else:
    c.table("brokers").insert({
        "id": "broker_001",
        "name": "Rahul Sharma",
        "phone": "9876543210",
        "email": "temp2saubhagya@gmail.com",
        "areas": ALL_LUCKNOW_AREAS,
        "is_active": True,
        "telegram_chat_id": "",
    }).execute()
    print("Broker created with {} areas".format(len(ALL_LUCKNOW_AREAS)))
