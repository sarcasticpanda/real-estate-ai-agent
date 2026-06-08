"""
Supabase client wrapper.
Reads SUPABASE_URL and SUPABASE_KEY from .env
"""

import os
from functools import lru_cache
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(url, key)


# ── Properties ──────────────────────────────────────────────────────────────

def upsert_property(doc: dict, semantic_text: str, embedding: list[float]) -> dict:
    """Insert or update a property record."""
    client = get_client()
    location = doc.get("location", {})
    pricing = doc.get("pricing", {})
    profile = doc.get("property_profile", {})

    row = {
        "id": doc["doc_id"],
        "property_id": doc["source_ids"]["property_id"],
        "data": doc,
        "semantic_text": semantic_text,
        "embedding": embedding,
        "area_name": location.get("area_name"),
        "city": location.get("city", "Lucknow"),
        "bhk": profile.get("bhk"),
        "price_inr": pricing.get("total_price_inr"),
        "property_type": profile.get("property_type"),
        "status": "available",
    }
    result = client.table("properties").upsert(row).execute()
    return result.data


def search_properties(
    query_embedding: list[float],
    match_threshold: float = 0.3,
    match_count: int = 10,
    filter_city: str | None = None,
    filter_max_price: int | None = None,
    filter_min_price: int | None = None,
    filter_bhk: int | None = None,
    filter_area: str | None = None,
) -> list[dict]:
    """Call the match_properties Postgres function for hybrid vector search."""
    client = get_client()
    params = {
        "query_embedding": query_embedding,
        "match_threshold": match_threshold,
        "match_count": match_count,
        "filter_city": filter_city,
        "filter_max_price": filter_max_price,
        "filter_min_price": filter_min_price,
        "filter_bhk": filter_bhk,
        "filter_area": filter_area,
    }
    result = client.rpc("match_properties", params).execute()
    return result.data or []


def mark_property_booked(property_id: str) -> None:
    client = get_client()
    client.rpc("mark_property_booked", {"prop_id": property_id}).execute()


# ── Leads ───────────────────────────────────────────────────────────────────

def save_lead(lead: dict) -> dict:
    client = get_client()
    result = client.table("leads").insert(lead).execute()
    return result.data[0] if result.data else {}


def update_lead_status(lead_id: str, status: str, notes: str | None = None) -> None:
    client = get_client()
    update = {"status": status}
    if notes:
        update["notes"] = notes
    client.table("leads").update(update).eq("id", lead_id).execute()


def get_leads_for_broker(broker_id: str, status: str | None = None) -> list[dict]:
    client = get_client()
    q = client.table("leads").select("*").eq("broker_id", broker_id)
    if status:
        q = q.eq("status", status)
    return q.execute().data or []


# ── Sessions ─────────────────────────────────────────────────────────────────

def get_session(session_id: str) -> dict:
    client = get_client()
    result = client.table("sessions").select("*").eq("session_id", session_id).execute()
    if result.data:
        return result.data[0]
    return {"session_id": session_id, "messages": [], "requirements": {}, "stage": "discovery"}


def save_session(session_id: str, messages: list, requirements: dict, stage: str) -> None:
    client = get_client()
    client.table("sessions").upsert({
        "session_id": session_id,
        "messages": messages,
        "requirements": requirements,
        "stage": stage,
    }).execute()


# ── Meetings ─────────────────────────────────────────────────────────────────

def save_meeting(meeting: dict) -> dict:
    client = get_client()
    result = client.table("meetings").insert(meeting).execute()
    return result.data[0] if result.data else {}


def get_upcoming_meetings(hours_ahead: int = 24) -> list[dict]:
    """Get meetings scheduled within the next N hours (for reminders)."""
    client = get_client()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    result = (
        client.table("meetings")
        .select("*, leads(*), brokers(*), properties(data)")
        .eq("status", "confirmed")
        .gte("scheduled_at", now.isoformat())
        .lte("scheduled_at", until.isoformat())
        .execute()
    )
    return result.data or []


# ── Brokers ──────────────────────────────────────────────────────────────────

def get_broker_for_area(area_name: str) -> dict | None:
    """Find an active broker who covers the given area."""
    client = get_client()
    result = (
        client.table("brokers")
        .select("*")
        .eq("is_active", True)
        .contains("areas", [area_name])
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_all_brokers() -> list[dict]:
    client = get_client()
    return client.table("brokers").select("*").eq("is_active", True).execute().data or []
