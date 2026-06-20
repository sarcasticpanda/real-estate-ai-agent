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
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) must be set in .env")
    return create_client(url, key)


# ── Properties ──────────────────────────────────────────────────────────────

def upsert_property(doc: dict, semantic_text: str, embedding: list[float]) -> dict:
    """Insert or update a property record. Preserves existing images if present in DB."""
    client = get_client()
    location = doc.get("location", {})
    pricing  = doc.get("pricing", {})
    profile  = doc.get("property_profile", {})

    # Preserve images already stored in Supabase (assign_fake_images.py writes them there).
    # When re-embedding from local JSON, the local file has no images field, so we keep DB images.
    merged_data = dict(doc)
    if not merged_data.get("images"):
        try:
            existing = client.table("properties").select("data").eq("id", doc["doc_id"]).execute()
            if existing.data:
                db_images = (existing.data[0].get("data") or {}).get("images")
                if db_images:
                    merged_data["images"] = db_images
        except Exception:
            pass

    row = {
        "id": doc["doc_id"],
        "property_id": doc["source_ids"]["property_id"],
        "data": merged_data,
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


def mark_property_sold(property_id: str) -> bool:
    client = get_client()
    result = client.table("properties").update({"status": "sold"}).eq("id", property_id).execute()
    return bool(result.data)


def list_properties(limit: int = 300, broker_id: str | None = None) -> list[dict]:
    """List properties (newest first) for the broker 'My listings' view."""
    client = get_client()
    rows = (client.table("properties")
            .select("id,property_id,area_name,city,bhk,price_inr,property_type,status,data,created_at")
            .order("created_at", desc=True).limit(limit).execute().data) or []
    if broker_id:
        rows = [r for r in rows
                if ((r.get("data") or {}).get("source_ids") or {}).get("broker_id") == broker_id]
    # Trim heavy/irrelevant data for the list view (keep images count + a couple fields)
    out = []
    for r in rows:
        data = r.get("data") or {}
        out.append({
            "id": r["id"], "area_name": r.get("area_name"), "city": r.get("city"),
            "bhk": r.get("bhk"), "price_inr": r.get("price_inr"),
            "property_type": r.get("property_type"), "status": r.get("status"),
            "images": len(data.get("images") or []),
            "documents": len(data.get("documents") or []),
            "address": (data.get("metadata") or {}).get("raw_full_address"),
            "furnishing": (data.get("property_profile") or {}).get("furnishing"),
            "area_sqft": (data.get("property_profile") or {}).get("builtup_area_sqft"),
            "description": ((data.get("metadata") or {}).get("description")
                            or data.get("description")),
            "amenities": data.get("amenities") or [],
            "broker": ((data.get("source_ids") or {}).get("broker_id")
                       or (data.get("metadata") or {}).get("broker_name")),
        })
    return out


def update_property(property_id: str, price_inr: int | None = None, status: str | None = None,
                    area_name: str | None = None, bhk: int | None = None,
                    property_type: str | None = None, furnishing: str | None = None,
                    area_sqft: int | None = None, description: str | None = None,
                    amenities: list[str] | None = None) -> bool:
    """Update any combination of listing fields; keeps data sub-fields in sync."""
    client = get_client()
    cur = client.table("properties").select("data,area_name,bhk,property_type").eq("id", property_id).execute().data
    if not cur:
        return False
    data = cur[0]["data"] or {}
    update: dict = {}

    if price_inr is not None:
        data.setdefault("pricing", {})["total_price_inr"] = int(price_inr)
        update["price_inr"] = int(price_inr)
    if status is not None:
        update["status"] = status
    if area_name is not None:
        update["area_name"] = area_name
        data.setdefault("location", {})["area_name"] = area_name
    if bhk is not None:
        update["bhk"] = int(bhk)
        data.setdefault("property_profile", {})["bhk"] = int(bhk)
    if property_type is not None:
        update["property_type"] = property_type
        data.setdefault("property_profile", {})["property_type"] = property_type
    if furnishing is not None:
        data.setdefault("property_profile", {})["furnishing"] = furnishing
    if area_sqft is not None:
        data.setdefault("property_profile", {})["builtup_area_sqft"] = int(area_sqft)
    if description is not None:
        data.setdefault("metadata", {})["description"] = description
    if amenities is not None:
        data["amenities"] = amenities

    if update or any(v is not None for v in [furnishing, area_sqft, description, amenities]):
        update["data"] = data
        client.table("properties").update(update).eq("id", property_id).execute()
    return True


def delete_property(property_id: str) -> bool:
    """
    Hard delete a property: removes all images from Supabase Storage,
    then deletes the DB row (embeddings cascade via FK or we ignore the orphan).
    """
    import urllib.parse
    client = get_client()
    cur = client.table("properties").select("data").eq("id", property_id).execute().data
    if not cur:
        return False
    data = cur[0]["data"] or {}
    documents = data.get("documents") or []

    def paths_for(items, bucket):
        paths = []
        for item in items:
            url = item.get("url") if isinstance(item, dict) else item
            if bucket not in (url or ""):
                continue
            parsed = urllib.parse.urlparse(url)
            parts = parsed.path.split(f"{bucket}/", 1)
            if len(parts) == 2:
                paths.append(urllib.parse.unquote(parts[1].split("?")[0]))
        return paths

    try:
        image_paths = paths_for(data.get("images") or [], IMAGES_BUCKET)
        document_paths = paths_for(documents, DOCS_BUCKET)
        if image_paths:
            client.storage.from_(IMAGES_BUCKET).remove(image_paths)
        if document_paths:
            client.storage.from_(DOCS_BUCKET).remove(document_paths)
    except Exception as e:
        raise RuntimeError(f"Storage cleanup failed; property was not deleted: {e}") from e

    client.table("properties").delete().eq("id", property_id).execute()
    return True



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


def get_recent_lead_by_phone(phone: str, within_hours: int = 48) -> dict | None:
    """Find a lead with this phone created within the last N hours (for dedup)."""
    if not phone:
        return None
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    client = get_client()
    rows = (client.table("leads").select("*").eq("phone", phone)
            .gte("created_at", since).order("created_at", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


def update_lead(lead_id: str, fields: dict) -> dict:
    """Update arbitrary fields on a lead (used by dedup to refresh an existing lead)."""
    client = get_client()
    res = client.table("leads").update(fields).eq("id", lead_id).execute()
    return res.data[0] if res.data else {}


def get_all_leads(status: str | None = None, limit: int = 200) -> list[dict]:
    """All leads, newest first — for the broker dashboard (leads may have no broker_id)."""
    client = get_client()
    q = client.table("leads").select("*").order("created_at", desc=True).limit(limit)
    if status and status != "all":
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


def update_meeting(meeting_id: str, fields: dict) -> dict:
    client = get_client()
    res = client.table("meetings").update(fields).eq("id", meeting_id).execute()
    return res.data[0] if res.data else {}


def get_lead(lead_id: str) -> dict | None:
    if not lead_id:
        return None
    client = get_client()
    rows = client.table("leads").select("*").eq("id", lead_id).limit(1).execute().data
    return rows[0] if rows else None


def meeting_slot_taken(dt_iso: str, window_minutes: int = 45, exclude_id: str | None = None) -> bool:
    """True if a (non-cancelled) visit is already booked within ±window of this time —
    a free, DB-based availability check so the agent can say 'is that slot free?'."""
    from datetime import datetime, timedelta
    try:
        dt = datetime.fromisoformat(dt_iso)
    except Exception:
        return False
    lo = (dt - timedelta(minutes=window_minutes)).isoformat()
    hi = (dt + timedelta(minutes=window_minutes)).isoformat()
    client = get_client()
    rows = (client.table("meetings").select("id,status")
            .gte("scheduled_at", lo).lte("scheduled_at", hi).execute().data) or []
    for r in rows:
        if r.get("id") == exclude_id:
            continue
        if (r.get("status") or "").lower() != "cancelled":
            return True
    return False


def list_meetings(limit: int = 100, status: str | None = None) -> list[dict]:
    """List all meetings (for broker dashboard), newest first."""
    client = get_client()
    q = (client.table("meetings")
         .select("id,lead_id,property_id,scheduled_at,status,notes,created_at")
         .order("scheduled_at", desc=True).limit(limit))
    if status:
        q = q.eq("status", status)
    meetings = q.execute().data or []
    # Enrich with lead name/phone
    for m in meetings:
        if m.get("lead_id"):
            try:
                lead = client.table("leads").select("name,phone,preferred_area").eq("id", m["lead_id"]).limit(1).execute().data
                if lead:
                    m["buyer_name"] = lead[0].get("name", "")
                    m["buyer_phone"] = lead[0].get("phone", "")
                    m["buyer_area"] = lead[0].get("preferred_area", "")
            except Exception:
                pass
    return meetings


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


def get_broker_by_phone(phone: str) -> dict | None:
    """Find an active broker while tolerating +91/91/local phone formatting."""
    normalized = _normalize_indian_phone(phone)
    for broker in get_all_brokers():
        if _normalize_indian_phone(broker.get("phone", "")) == normalized:
            return broker
    return None


# ── Property Images (Supabase Storage) ───────────────────────────────────────

IMAGES_BUCKET = "property-images"


def upload_property_image(property_id: str, filename: str, file_bytes: bytes, content_type: str = "image/jpeg") -> str | None:
    """
    Upload image bytes to Supabase Storage.
    Returns the public URL or None on failure.
    Bucket 'property-images' must exist and be set to public in Supabase dashboard.
    """
    client = get_client()
    storage_path = f"{property_id}/{filename}"

    try:
        client.storage.from_(IMAGES_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        public_url = client.storage.from_(IMAGES_BUCKET).get_public_url(storage_path)
        logger.info(f"Image uploaded: {storage_path} → {public_url}")
        return public_url
    except Exception as e:
        logger.error(f"Image upload failed for {storage_path}: {e}")
        return None


def add_image_url_to_property(property_id: str, image_url: str) -> bool:
    """Append image URL to the property's data.images list."""
    client = get_client()
    try:
        result = client.table("properties").select("data").eq("id", property_id).execute()
        if not result.data:
            return False

        data = result.data[0]["data"]
        images = data.get("images") or []
        if image_url not in images:
            images.append(image_url)
            data["images"] = images
            client.table("properties").update({"data": data}).eq("id", property_id).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to add image URL to property {property_id}: {e}")
        return False


def get_property_images(property_id: str) -> list[str]:
    """Return the list of image URLs for a property."""
    client = get_client()
    result = client.table("properties").select("data").eq("id", property_id).execute()
    if not result.data:
        return []
    return result.data[0]["data"].get("images") or []


def delete_property_image(property_id: str, image_url: str) -> bool:
    """Remove one image URL from data.images and delete from Storage if it's our bucket."""
    import urllib.parse
    client = get_client()
    result = client.table("properties").select("data").eq("id", property_id).execute()
    if not result.data:
        return False
    data = result.data[0]["data"]
    images = [u for u in (data.get("images") or []) if u != image_url]
    data["images"] = images
    client.table("properties").update({"data": data}).eq("id", property_id).execute()
    # Also delete from Storage if it's in our bucket
    if IMAGES_BUCKET in (image_url or ""):
        try:
            parsed = urllib.parse.urlparse(image_url)
            # path looks like /storage/v1/object/public/property-images/<property_id>/<filename>
            parts = parsed.path.split(f"{IMAGES_BUCKET}/", 1)
            if len(parts) == 2:
                storage_path = parts[1].split("?")[0]
                client.storage.from_(IMAGES_BUCKET).remove([storage_path])
        except Exception as e:
            import logging; logging.getLogger(__name__).warning(f"Storage delete failed: {e}")
    return True


def reorder_property_images(property_id: str, ordered_urls: list[str]) -> bool:
    """Replace the images list with a caller-supplied ordering (first = hero)."""
    client = get_client()
    result = client.table("properties").select("data").eq("id", property_id).execute()
    if not result.data:
        return False
    data = result.data[0]["data"]
    existing = data.get("images") or []
    if len(ordered_urls) != len(existing) or set(ordered_urls) != set(existing):
        return False
    data["images"] = ordered_urls
    client.table("properties").update({"data": data}).eq("id", property_id).execute()
    return True


def replace_unsplash_images(property_id: str, new_urls: list[str]) -> bool:
    """When broker uploads real images, drop all old Unsplash placeholders."""
    client = get_client()
    result = client.table("properties").select("data").eq("id", property_id).execute()
    if not result.data:
        return False
    data = result.data[0]["data"]
    existing = data.get("images") or []
    # Keep only our own Storage URLs; drop Unsplash
    own = [u for u in existing if "unsplash.com" not in u]
    data["images"] = own + new_urls
    client.table("properties").update({"data": data}).eq("id", property_id).execute()
    return True


# ── Property documents (floor plans, brochures, papers) ──────────────────────
DOCS_BUCKET = "property-documents"


def upload_property_document(property_id: str, filename: str, file_bytes: bytes, content_type: str = "application/pdf") -> str | None:
    """
    Upload a document (PDF/image) to Supabase Storage. Returns the public URL or None.
    Bucket 'property-documents' must exist and be public in the Supabase dashboard.
    """
    client = get_client()
    storage_path = f"{property_id}/{filename}"
    try:
        client.storage.from_(DOCS_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        public_url = client.storage.from_(DOCS_BUCKET).get_public_url(storage_path)
        logger.info(f"Document uploaded: {storage_path}")
        return public_url
    except Exception as e:
        logger.error(f"Document upload failed for {storage_path}: {e}")
        return None


def add_document_to_property(property_id: str, doc_url: str, label: str | None = None) -> bool:
    """Append a document {url, label} to the property's data.documents list."""
    client = get_client()
    try:
        result = client.table("properties").select("data").eq("id", property_id).execute()
        if not result.data:
            return False
        data = result.data[0]["data"]
        docs = data.get("documents") or []
        if not any(d.get("url") == doc_url for d in docs):
            docs.append({"url": doc_url, "label": label or "Document"})
            data["documents"] = docs
            client.table("properties").update({"data": data}).eq("id", property_id).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to add document to property {property_id}: {e}")
        return False


def get_property_documents(property_id: str) -> list[dict]:
    """Return the list of {url, label} documents for a property."""
    client = get_client()
    result = client.table("properties").select("data").eq("id", property_id).execute()
    if not result.data:
        return []
    return result.data[0]["data"].get("documents") or []


import logging
logger = logging.getLogger(__name__)


# ── User Profiles (stored inside session requirements._profile) ───────────────

def get_user_profile(session_id: str) -> dict:
    """Return stored user profile dict (name, phone, email, onboarding_step)."""
    client = get_client()
    result = client.table("sessions").select("requirements").eq("session_id", session_id).execute()
    if result.data:
        req = result.data[0].get("requirements") or {}
        return req.get("_profile") or {}
    return {}


def save_broker_confirmation(data: dict) -> dict:
    """
    Store a pending broker availability confirmation.
    Keys: lead_id, meeting_id, broker_phone, buyer_name, buyer_phone,
          property_id, proposed_dt (ISO), buyer_session_id, status (pending/yes/no)
    """
    client = get_client()
    payload = dict(data)
    payload["broker_phone"] = _normalize_indian_phone(payload.get("broker_phone", ""))
    if payload.get("buyer_phone"):
        payload["buyer_phone"] = _normalize_indian_phone(payload["buyer_phone"])
    payload["status"] = "pending"
    res = client.table("broker_confirmations").insert(payload).execute()
    return res.data[0] if res.data else {}


def get_pending_broker_confirmation(broker_phone: str) -> dict | None:
    """Get the most recent pending confirmation for this broker phone."""
    client = get_client()
    phone = _normalize_indian_phone(broker_phone)
    rows = (client.table("broker_confirmations").select("*")
            .eq("broker_phone", phone).eq("status", "pending")
            .order("created_at", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


def get_latest_broker_confirmation(broker_phone: str, buyer_phone: str | None = None) -> dict | None:
    """Return the latest confirmed/rescheduled visit associated with a broker."""
    client = get_client()
    phone = _normalize_indian_phone(broker_phone)
    query = (client.table("broker_confirmations").select("*")
             .eq("broker_phone", phone).in_("status", ["yes", "rescheduled"]))
    if buyer_phone:
        query = query.eq("buyer_phone", _normalize_indian_phone(buyer_phone))
    rows = query.order("created_at", desc=True).limit(1).execute().data
    return rows[0] if rows else None


def update_broker_confirmation(conf_id: str, status: str | None = None,
                               fields: dict | None = None) -> None:
    client = get_client()
    update = dict(fields or {})
    if status is not None:
        update["status"] = status
    if update:
        client.table("broker_confirmations").update(update).eq("id", conf_id).execute()


def _normalize_indian_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return digits


def save_user_profile(session_id: str, profile: dict) -> None:
    """Persist user profile into session requirements._profile without overwriting other fields."""
    client = get_client()
    result = client.table("sessions").select("requirements").eq("session_id", session_id).execute()
    if result.data:
        req = result.data[0].get("requirements") or {}
        req["_profile"] = profile
        client.table("sessions").update({"requirements": req}).eq("session_id", session_id).execute()
    else:
        client.table("sessions").insert({
            "session_id": session_id,
            "messages": [],
            "requirements": {"_profile": profile},
            "stage": "discovery",
        }).execute()
