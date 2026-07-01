"""
FastAPI application — the central API server.

Endpoints:
  POST /chat                    — user sends a message, gets AI reply
  POST /shortlist               — save a property to session shortlist
  GET  /shortlist/{session_id}  — retrieve user's saved properties
  POST /upload                  — broker uploads a CSV file
  GET  /properties              — list/search properties
  POST /webhook/n8n/lead        — n8n calls this after lead notification
  POST /webhook/n8n/meeting     — n8n calls this after meeting scheduled
  POST /webhook/telegram        — Telegram updates (webhook mode when hosted)
  GET  /webhook/whatsapp        — Meta webhook verification
  POST /webhook/whatsapp        — inbound WhatsApp messages

Run:
    uvicorn api.main:app --reload --port 8000
"""

import os
import sys
import logging
import tempfile
import uuid
import re
import json
import hmac
import hashlib
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from agent.property_agent import process_message
from broker.upload_handler import (
    process_csv, create_property_from_fields, reenrich_property_location,
    refresh_property_index,
)
from database.supabase_client import (
    update_lead_status, save_meeting, get_upcoming_meetings,
    upload_property_image, add_image_url_to_property, get_property_images,
    delete_property_image, reorder_property_images, replace_unsplash_images, delete_property,
    upload_property_document, add_document_to_property, get_property_documents,
    get_session, save_session, get_all_leads, mark_property_sold,
    list_properties, update_property,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Real Estate AI Agent",
    description="Property discovery and broker automation platform",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Chat endpoint ────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str
    platform: str = "web"


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    properties: list = []


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Send a message to the property AI assistant."""
    try:
        result = process_message(
            session_id=req.session_id,
            user_message=req.message,
            platform=req.platform,
        )
        return ChatResponse(
            session_id=req.session_id,
            reply=result["reply"],
            properties=result.get("properties", []),
        )
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Shortlist endpoints ───────────────────────────────────────────────────────

class ShortlistRequest(BaseModel):
    session_id: str
    property_id: str


@app.post("/shortlist")
async def save_to_shortlist(req: ShortlistRequest):
    """Add a property to the user's session shortlist."""
    try:
        session = get_session(req.session_id)
        requirements = session.get("requirements") or {}
        shortlist = requirements.get("_shortlist") or []

        if req.property_id not in shortlist:
            shortlist.append(req.property_id)
            requirements["_shortlist"] = shortlist
            save_session(
                session_id=req.session_id,
                messages=session.get("messages") or [],
                requirements=requirements,
                stage=session.get("stage") or "discovery",
            )
            return {"ok": True, "saved": True, "count": len(shortlist)}
        return {"ok": True, "saved": False, "count": len(shortlist), "note": "already saved"}
    except Exception as e:
        logger.error(f"Shortlist save error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/shortlist")
async def remove_from_shortlist(req: ShortlistRequest):
    """Remove a property from the user's session shortlist."""
    try:
        session = get_session(req.session_id)
        requirements = session.get("requirements") or {}
        shortlist = requirements.get("_shortlist") or []

        if req.property_id in shortlist:
            shortlist.remove(req.property_id)
            requirements["_shortlist"] = shortlist
            save_session(
                session_id=req.session_id,
                messages=session.get("messages") or [],
                requirements=requirements,
                stage=session.get("stage") or "discovery",
            )
        return {"ok": True, "count": len(shortlist)}
    except Exception as e:
        logger.error(f"Shortlist remove error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/shortlist/{session_id}")
async def get_shortlist(session_id: str):
    """Get the user's saved/shortlisted properties."""
    try:
        from rag.retriever import to_card
        from database.supabase_client import get_client

        session = get_session(session_id)
        requirements = session.get("requirements") or {}
        shortlist = requirements.get("_shortlist") or []

        if not shortlist:
            return {"count": 0, "properties": []}

        client = get_client()
        result = client.table("properties").select("*").in_("id", shortlist).execute()
        if not result.data:
            return {"count": 0, "properties": []}

        cards = [
            to_card({"id": r["id"], "data": r["data"], "score": 0, "similarity": 1.0})
            for r in result.data
        ]
        return {"count": len(cards), "properties": cards}
    except Exception as e:
        logger.error(f"Shortlist fetch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Customer auth (email OTP + Google) ────────────────────────────────────────

class OtpRequest(BaseModel):
    email: str

class OtpVerify(BaseModel):
    email: str
    code: str

class GoogleAuth(BaseModel):
    credential: str

class FavReq(BaseModel):
    property_id: str


def _current_customer(authorization: str) -> dict | None:
    from api.auth import verify_jwt, get_customer
    claims = verify_jwt(authorization or "")
    if not claims:
        return None
    return get_customer(claims.get("sub"))


@app.get("/auth/config")
async def auth_config():
    """Public client-side config (the Google Client ID is not a secret)."""
    return {"google_client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")}


@app.post("/auth/otp/request")
async def auth_otp_request(req: OtpRequest):
    from api.auth import request_otp
    return {"ok": request_otp(req.email)}


@app.post("/auth/otp/verify")
async def auth_otp_verify(req: OtpVerify):
    from api.auth import verify_otp, get_or_create_customer, issue_jwt
    if not verify_otp(req.email, req.code):
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    cust = get_or_create_customer(req.email)
    return {"token": issue_jwt(cust), "customer": {"email": cust["email"], "name": cust.get("name")}}


@app.post("/auth/google")
async def auth_google(req: GoogleAuth):
    from api.auth import verify_google, get_or_create_customer, issue_jwt
    info = verify_google(req.credential)
    if not info:
        raise HTTPException(status_code=401, detail="Google sign-in failed (is GOOGLE_OAUTH_CLIENT_ID set?)")
    cust = get_or_create_customer(info["email"], info.get("name"), info.get("sub"))
    return {"token": issue_jwt(cust), "customer": {"email": cust["email"], "name": cust.get("name")}}


@app.get("/me")
async def me(authorization: str = Header(default="")):
    cust = _current_customer(authorization)
    if not cust:
        raise HTTPException(status_code=401, detail="Not logged in")
    return {"email": cust["email"], "name": cust.get("name"),
            "phone": cust.get("phone"), "favourites": cust.get("favourites") or []}


@app.post("/me/favourites")
async def add_favourite(req: FavReq, authorization: str = Header(default="")):
    from api.auth import set_customer_favourites
    cust = _current_customer(authorization)
    if not cust:
        raise HTTPException(status_code=401, detail="Not logged in")
    favs = list(cust.get("favourites") or [])
    if req.property_id not in favs:
        favs.append(req.property_id)
        set_customer_favourites(cust["id"], favs)
    return {"ok": True, "count": len(favs)}


@app.delete("/me/favourites")
async def remove_favourite(req: FavReq, authorization: str = Header(default="")):
    from api.auth import set_customer_favourites
    cust = _current_customer(authorization)
    if not cust:
        raise HTTPException(status_code=401, detail="Not logged in")
    favs = [p for p in (cust.get("favourites") or []) if p != req.property_id]
    set_customer_favourites(cust["id"], favs)
    return {"ok": True, "count": len(favs)}


@app.get("/me/properties")
async def my_properties(authorization: str = Header(default="")):
    """Everything this customer liked + their booked visits (for the My Properties page)."""
    cust = _current_customer(authorization)
    if not cust:
        raise HTTPException(status_code=401, detail="Not logged in")
    from rag.retriever import to_card
    from database.supabase_client import get_client
    c = get_client()

    favs = cust.get("favourites") or []
    cards = []
    if favs:
        rows = c.table("properties").select("*").in_("id", favs).execute().data or []
        cards = [to_card({"id": r["id"], "data": r["data"], "score": 0, "similarity": 1.0}) for r in rows]

    visits = []
    phone = cust.get("phone")
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
        leads = c.table("leads").select("id").ilike("phone", f"%{digits}%").execute().data or []
        lead_ids = [l["id"] for l in leads]
        if lead_ids:
            mts = (c.table("meetings").select("*").in_("lead_id", lead_ids)
                   .neq("status", "cancelled").order("scheduled_at").execute().data) or []
            visits = [{"scheduled_at": m.get("scheduled_at"), "status": m.get("status"),
                       "property_id": m.get("property_id")} for m in mts]

    return {"name": cust.get("name"), "email": cust["email"], "favourites": cards, "visits": visits}


# ── Broker upload endpoint ────────────────────────────────────────────────────

_UPLOAD_JOBS: dict[str, dict] = {}
MAX_CSV_SIZE_MB = 10


@app.post("/upload/preview")
async def upload_preview(token: str = Form(...), file: UploadFile = File(...)):
    """
    Parse a broker CSV and return a preview of detected columns + sample rows
    WITHOUT importing. Broker reviews, corrects if needed, then calls /upload.
    """
    _check_broker_token(token)
    import csv, io, chardet
    raw = await file.read()
    if len(raw) > MAX_CSV_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"CSV too large - max {MAX_CSV_SIZE_MB} MB")
    enc = chardet.detect(raw).get("encoding") or "utf-8"
    text = raw.decode(enc, errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)[:5]  # only first 5 for preview
    headers = reader.fieldnames or []

    # Auto-map column names using flexible aliases
    ALIASES = {
        "property_type": ["property_type","type","prop_type","category","kind"],
        "bhk": ["bhk","bedrooms","beds","bedroom","rooms","bed"],
        "price_inr": ["price_inr","price","cost","amount","value","rate","total_price"],
        "area_sqft": ["area_sqft","area","sqft","size","carpet_area","builtup","built_up"],
        "address": ["address","locality","location","addr","area_name","area","neighbourhood"],
        "city": ["city","town"],
        "furnishing": ["furnishing","furnished","furnish"],
        "amenities": ["amenities","facilities","features","amenity"],
        "broker_name": ["broker_name","broker","agent","agent_name","owner"],
        "broker_phone": ["broker_phone","phone","mobile","contact","number"],
        "description": ["description","desc","details","remarks","notes"],
        "external_ref": ["external_ref","ref","listing_id","id","property_id","pid"],
    }
    mapping = {}
    h_lower = {h.lower().strip().replace(" ", "_"): h for h in headers}
    for canon, alts in ALIASES.items():
        for a in alts:
            if a in h_lower:
                mapping[canon] = h_lower[a]
                break

    return {
        "ok": True,
        "total_rows": sum(1 for _ in csv.DictReader(io.StringIO(text))),
        "headers": headers,
        "column_mapping": mapping,
        "sample_rows": rows[:3],
        "unmapped": [c for c in ["property_type","price_inr","address"] if c not in mapping],
    }


@app.post("/upload")
async def upload_properties(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    broker_id: str = Form(None),
    token: str = Form(None),
    column_map: str = Form(None),   # JSON string: {"price_inr": "Cost", "address": "Locality", ...}
):
    """
    Import properties from a CSV. Supports any column names via column_map.
    Flow: broker calls /upload/preview first to get the auto-detected mapping,
    reviews/corrects it, then submits here with column_map as JSON.
    Geocoding (lat/lng) + Overpass POI distances run automatically per property.
    """
    _check_broker_token(token)
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted")

    content = await file.read()
    if len(content) > MAX_CSV_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"CSV too large - max {MAX_CSV_SIZE_MB} MB")
    import csv as _csv, io as _io, chardet as _chardet
    encoding = _chardet.detect(content).get("encoding") or "utf-8"
    csv_text = content.decode(encoding, errors="replace")
    parsed_rows = list(_csv.DictReader(_io.StringIO(csv_text)))
    headers = _csv.DictReader(_io.StringIO(csv_text)).fieldnames or []
    if not headers:
        raise HTTPException(status_code=400, detail="CSV has no header row")
    if not parsed_rows:
        raise HTTPException(status_code=400, detail="CSV has no property rows")
    if len(parsed_rows) > 500:
        raise HTTPException(status_code=400, detail="CSV may contain at most 500 rows per import")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="wb")
    tmp.write(csv_text.encode("utf-8"))
    tmp.close()

    parsed_map = {}
    if column_map:
        import json as _json
        try:
            parsed_map = _json.loads(column_map)
        except Exception as e:
            Path(tmp.name).unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Invalid column mapping: {e}")
        if not isinstance(parsed_map, dict):
            Path(tmp.name).unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Column mapping must be a JSON object")
        allowed_fields = {"property_type", "bhk", "price_inr", "area_sqft", "address", "city",
                          "furnishing", "amenities", "broker_name", "broker_phone",
                          "description", "external_ref"}
        if any(k not in allowed_fields or v not in headers for k, v in parsed_map.items()):
            Path(tmp.name).unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Column mapping contains an unknown field or CSV column")
        missing = {"property_type", "price_inr", "address"} - set(parsed_map)
        if missing:
            Path(tmp.name).unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Required mappings missing: {', '.join(sorted(missing))}")

    job_id = uuid.uuid4().hex
    _UPLOAD_JOBS[job_id] = {"status": "processing", "filename": file.filename}
    background_tasks.add_task(_run_upload, job_id, tmp.name, broker_id, parsed_map)

    return {
        "status": "processing",
        "message": f"File '{file.filename}' received. Each property will be geocoded and enriched with POI distances. This takes ~2s per row.",
        "broker_id": broker_id,
        "job_id": job_id,
    }


@app.get("/upload/status/{job_id}")
async def upload_status(job_id: str, token: str):
    _check_broker_token(token)
    job = _UPLOAD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Upload job not found")
    return job


def _run_upload(job_id: str, filepath: str, broker_id: str | None,
                column_map: dict | None = None) -> None:
    """Run the full enrichment pipeline on a CSV file.
    column_map remaps arbitrary column names to canonical ones before processing."""
    import csv, io
    try:
        if column_map:
            # Re-write the file with canonical column names so process_csv understands it
            with open(filepath, encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            if rows:
                # Build reverse map: original_header → canonical_name
                rev = {v: k for k, v in column_map.items()}
                remapped = []
                for row in rows:
                    new_row = {}
                    for k, v in row.items():
                        new_row[rev.get(k, k)] = v
                    remapped.append(new_row)
                import tempfile as _tmp
                with _tmp.NamedTemporaryFile(delete=False, suffix=".csv", mode="w",
                                             encoding="utf-8", newline="") as out:
                    w = csv.DictWriter(out, fieldnames=list(remapped[0].keys()))
                    w.writeheader(); w.writerows(remapped)
                    mapped_path = out.name
                Path(filepath).unlink(missing_ok=True)
                filepath = mapped_path

        result = process_csv(filepath, broker_id=broker_id)
        _UPLOAD_JOBS[job_id] = {"status": "complete", **result}
        logger.info(f"Upload job complete: {result}")
    except Exception as e:
        _UPLOAD_JOBS[job_id] = {"status": "failed", "error": str(e)}
        logger.error(f"Upload pipeline error: {e}")
    finally:
        Path(filepath).unlink(missing_ok=True)


# ── Properties search endpoint ────────────────────────────────────────────────

@app.get("/properties")
async def get_properties(
    query: str = "",
    city: str = "Lucknow",
    bhk: int = None,
    max_budget_cr: float = None,
    area: str = None,
    limit: int = 10,
):
    from rag.retriever import retrieve

    requirements = {
        "city": city,
        "bhk": bhk,
        "max_budget_cr": max_budget_cr,
        "area": area,
    }
    results = retrieve(query or f"property in {city}", requirements, top_k=limit)
    return {"count": len(results), "results": [r["data"] for r in results]}


# ── n8n webhook endpoints ─────────────────────────────────────────────────────

class LeadWebhook(BaseModel):
    lead_id: str
    status: str
    notes: str = None


@app.post("/webhook/n8n/lead")
async def lead_webhook(payload: LeadWebhook):
    update_lead_status(payload.lead_id, payload.status, payload.notes)
    return {"ok": True}


class MeetingWebhook(BaseModel):
    lead_id: str
    broker_id: str
    property_id: str
    scheduled_at: str
    duration_minutes: int = 60


@app.post("/webhook/n8n/meeting")
async def meeting_webhook(payload: MeetingWebhook):
    from database.supabase_client import get_client

    meeting = save_meeting({
        "lead_id": payload.lead_id,
        "broker_id": payload.broker_id,
        "property_id": payload.property_id,
        "scheduled_at": payload.scheduled_at,
        "duration_minutes": payload.duration_minutes,
        "status": "confirmed",
    })

    calendar_link = None
    try:
        from notifications.calendar_integration import send_calendar_invite
        client = get_client()
        lead_rows   = client.table("leads").select("*").eq("id", payload.lead_id).execute()
        broker_rows = client.table("brokers").select("*").eq("id", payload.broker_id).execute()
        lead   = lead_rows.data[0]   if lead_rows.data   else {}
        broker = broker_rows.data[0] if broker_rows.data else {}
        calendar_link = send_calendar_invite(meeting, lead, broker)
    except Exception as e:
        logger.warning(f"Calendar integration skipped: {e}")

    return {"ok": True, "meeting_id": meeting.get("id"), "calendar_link": calendar_link}


@app.get("/meetings/upcoming")
async def upcoming_meetings(token: str, hours_ahead: int = 24):
    _check_broker_token(token)
    meetings = get_upcoming_meetings(hours_ahead)
    return {"count": len(meetings), "meetings": meetings}


@app.get("/broker/meetings/api")
async def broker_meetings_api(token: str, status: str = ""):
    """All meetings for broker dashboard, with buyer details."""
    _check_broker_token(token)
    from database.supabase_client import list_meetings
    rows = list_meetings(limit=200, status=status or None)
    return {"meetings": rows, "count": len(rows)}


@app.post("/broker/meetings/{meeting_id}/reschedule")
async def broker_reschedule_meeting(meeting_id: str, req: dict):
    """Broker reschedules a meeting from the dashboard — updates DB + notifies buyer."""
    from pydantic import BaseModel as BM
    token = req.get("token",""); new_time = req.get("new_time","")
    _check_broker_token(token)
    if not new_time:
        raise HTTPException(status_code=400, detail="new_time required")
    from agent.property_agent import _parse_visit_time, _send_visit_confirmation_email, _gcal_link
    from database.supabase_client import update_meeting, get_client
    from notifications.whatsapp_notifier import _send
    dt, when = _parse_visit_time(new_time)
    update_fields = {"status": "rescheduled"}
    if dt:
        update_fields["scheduled_at"] = dt.isoformat()
    update_meeting(meeting_id, update_fields)
    # Try to notify buyer
    try:
        cl = get_client()
        mtg = cl.table("meetings").select("lead_id").eq("id", meeting_id).limit(1).execute().data
        if mtg and mtg[0].get("lead_id"):
            lead = cl.table("leads").select("name,phone,email,session_id").eq("id", mtg[0]["lead_id"]).limit(1).execute().data
            if lead:
                l = lead[0]
                if l.get("phone"):
                    _send(l["phone"], f"Hi {l.get('name','')}, your visit has been rescheduled to *{when}*. See you then!")
                if l.get("email") and dt:
                    gcal = _gcal_link(dt, "Property visit", f"Rescheduled to {when}", "Lucknow")
                    _send_visit_confirmation_email(l["email"], l.get("name",""), when, "Lucknow", gcal, dt)
    except Exception as e:
        logger.warning(f"Notification after reschedule failed: {e}")
    return {"ok": True, "new_time": when}


@app.post("/broker/meetings/{meeting_id}/confirm")
async def broker_confirm_meeting(meeting_id: str, req: dict):
    """Broker confirms a visit from the dashboard — mirrors the WhatsApp YES."""
    _check_broker_token(req.get("token", ""))
    from datetime import datetime
    from database.supabase_client import update_meeting, get_client, update_lead
    from notifications.whatsapp_notifier import _send
    from agent.property_agent import _send_visit_confirmation_email, _gcal_link, _fmt_visit
    cl = get_client()
    rows = cl.table("meetings").select("*").eq("id", meeting_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="meeting not found")
    m = rows[0]
    update_meeting(meeting_id, {"status": "confirmed"})
    dt, when = None, "your visit"
    if m.get("scheduled_at"):
        try:
            dt = datetime.fromisoformat(m["scheduled_at"]); when = _fmt_visit(dt)
        except Exception:
            pass
    if dt:
        try:
            from notifications.calendar_client import add_event_to_broker_calendar
            add_event_to_broker_calendar(dt, summary="Property visit",
                                         description="Confirmed via broker dashboard", duration_minutes=60)
        except Exception as e:
            logger.debug(f"calendar add on web-confirm skipped: {e}")
    if m.get("lead_id"):
        lead = cl.table("leads").select("name,phone,email").eq("id", m["lead_id"]).limit(1).execute().data
        if lead:
            l = lead[0]
            if l.get("phone"):
                _send(l["phone"], f"Great news, {l.get('name','')}! Your visit on *{when}* is confirmed. See you then! 🎉")
            if l.get("email") and dt:
                gcal = _gcal_link(dt, "Property visit", f"Confirmed for {when}", "Lucknow")
                _send_visit_confirmation_email(l["email"], l.get("name", ""), when, "Lucknow", gcal, dt)
        update_lead(m["lead_id"], {"status": "visit"})
    return {"ok": True, "when": when}


@app.post("/broker/meetings/{meeting_id}/decline")
async def broker_decline_meeting(meeting_id: str, req: dict):
    """Broker declines a visit from the dashboard — mirrors the WhatsApp NO."""
    _check_broker_token(req.get("token", ""))
    from database.supabase_client import update_meeting, get_client
    from notifications.whatsapp_notifier import _send
    cl = get_client()
    rows = cl.table("meetings").select("*").eq("id", meeting_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="meeting not found")
    m = rows[0]
    update_meeting(meeting_id, {"status": "cancelled", "notes": "Declined by broker via dashboard"})
    if m.get("lead_id"):
        lead = cl.table("leads").select("name,phone").eq("id", m["lead_id"]).limit(1).execute().data
        if lead and lead[0].get("phone"):
            _send(lead[0]["phone"],
                  f"Hi {lead[0].get('name','')}, our consultant isn't free at that time. "
                  "Could you suggest another day/time? I'll check availability right away.")
    return {"ok": True}


@app.get("/api/broker/calendar/busy")
async def broker_calendar_busy(token: str, days: int = 7):
    """Return broker's Google Calendar busy slots for the next N days."""
    _check_broker_token(token)
    from datetime import datetime, timezone
    try:
        from notifications.calendar_client import get_broker_busy_slots
        slots = get_broker_busy_slots(datetime.now(timezone.utc), days=days)
        return {"configured": True, "busy_slots": slots}
    except Exception as e:
        return {"configured": False, "busy_slots": [], "note": str(e)}


@app.get("/broker/meetings", response_class=HTMLResponse)
async def broker_meetings_page():
    content = """
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center">
  <select id="f-status" class="input" style="width:160px" onchange="load()">
    <option value="">All meetings</option>
    <option value="pending">Pending</option>
    <option value="confirmed">Confirmed</option>
    <option value="rescheduled">Rescheduled</option>
    <option value="cancelled">Cancelled</option>
  </select>
  <span id="count" style="font-size:13px;color:#64748b;margin-left:8px"></span>
</div>
<div id="mtgs"></div>
"""
    scripts = """<script>
async function load(){
  const t=tok();if(!t){document.getElementById('mtgs').innerHTML='<p style="color:#94a3b8">Enter broker token in sidebar.</p>';return;}
  const st=document.getElementById('f-status').value;
  const r=await fetch('/broker/meetings/api?token='+encodeURIComponent(t)+(st?'&status='+st:''));
  const d=await r.json(); const ms=d.meetings||[];
  document.getElementById('count').textContent=ms.length+' meetings';
  if(!ms.length){document.getElementById('mtgs').innerHTML='<div class="empty"><div class="empty-icon">📅</div><p>No meetings yet.</p></div>';return;}
  const STATUS_COLOR={'confirmed':'badge-won','pending':'badge-new','rescheduled':'badge-visit','cancelled':'badge-lost'};
  document.getElementById('mtgs').innerHTML='<div class="card"><table class="table"><thead><tr><th>Buyer</th><th>Phone</th><th>Area</th><th>Scheduled</th><th>Status</th><th>Actions</th></tr></thead><tbody>'
    +ms.map(m=>{
      const dt=m.scheduled_at?new Date(m.scheduled_at).toLocaleString('en-IN',{dateStyle:'medium',timeStyle:'short'}):'—';
      const phone=(m.buyer_phone||'').replace(/[^0-9]/g,'');
      const bs='font-size:11px;padding:4px 9px;border:0;border-radius:8px;cursor:pointer;margin-right:4px';
      const pending=(m.status==='pending'||m.status==='rescheduled');
      const actions=(pending?`<button style="${bs};background:#16a34a;color:#fff" onclick="confirmMtg('${m.id}')">✓ Confirm</button><button style="${bs};background:#ef4444;color:#fff" onclick="declineMtg('${m.id}')">✕ Decline</button>`:'')
        +`<button class="btn btn-ghost" style="font-size:11px;padding:4px 9px" onclick="reschedule('${m.id}')">Reschedule</button>`;
      return `<tr>
        <td><b>${m.buyer_name||'Unknown'}</b></td>
        <td><a href="https://wa.me/91${phone}" target="_blank" style="color:#059669;font-weight:600">${m.buyer_phone||'—'}</a></td>
        <td>${m.buyer_area||'—'}</td>
        <td>${dt}</td>
        <td><span class="badge ${STATUS_COLOR[m.status]||'badge-wait'}">${m.status}</span></td>
        <td>${actions}</td>
      </tr>`;
    }).join('')+'</tbody></table></div>';
}
async function confirmMtg(id){
  if(!confirm('Confirm this visit? The buyer will be notified and it will be added to your calendar.'))return;
  const r=await fetch('/broker/meetings/'+id+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok()})});
  const d=await r.json(); if(d.ok){toast('Confirmed'+(d.when?' for '+d.when:'')+' — buyer notified ✅');load();} else toast('Error: '+(d.detail||'unknown'),'false');
}
async function declineMtg(id){
  if(!confirm('Decline this visit? The buyer will be asked to pick another time.'))return;
  const r=await fetch('/broker/meetings/'+id+'/decline',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok()})});
  const d=await r.json(); if(d.ok){toast('Declined — buyer asked to reschedule');load();} else toast('Error: '+(d.detail||'unknown'),'false');
}
async function reschedule(id){
  const t=prompt('Enter new date and time (e.g. "Saturday 5pm" or "25 Jun at 4pm"):');
  if(!t)return;
  const r=await fetch('/broker/meetings/'+id+'/reschedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok(),new_time:t})});
  const d=await r.json();
  if(d.ok){toast('Rescheduled to '+d.new_time+' — buyer notified');load();}
  else toast('Error: '+(d.detail||'unknown'),'false');
}
document.getElementById('tok-input').addEventListener('change',load);
load();
</script>"""
    return _broker_page("Meetings & Visits", content, scripts=scripts,
        hdr_extra='<a href="/broker/pipeline" class="btn btn-ghost" style="font-size:12px;padding:7px 14px">🎯 Pipeline</a>')


# ── Property image upload ─────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_SIZE_MB = 5


@app.post("/properties/{property_id}/images")
async def upload_image(property_id: str, token: str = Form(...), file: UploadFile = File(...)):
    _check_broker_token(token)
    if not get_property_images(property_id) and not _property_exists(property_id):
        raise HTTPException(status_code=404, detail="Property not found")
    content_type = file.content_type or "image/jpeg"
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {content_type}. Use JPEG/PNG/WebP.")

    file_bytes = await file.read()
    if len(file_bytes) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Image too large — max {MAX_IMAGE_SIZE_MB} MB")

    filename = _safe_image_filename(file.filename, content_type)

    public_url = upload_property_image(property_id, filename, file_bytes, content_type)
    if not public_url:
        raise HTTPException(status_code=500, detail="Image upload failed — check Supabase Storage bucket exists")

    add_image_url_to_property(property_id, public_url)

    return {"ok": True, "property_id": property_id, "image_url": public_url, "filename": filename}


@app.get("/properties/{property_id}/images")
async def get_images(property_id: str):
    images = get_property_images(property_id)
    return {"property_id": property_id, "count": len(images), "images": images}


@app.post("/properties/{property_id}/images/multi")
async def upload_images_multi(property_id: str, token: str = Form(...), files: list[UploadFile] = File(...)):
    """Upload multiple images at once. Drops Unsplash placeholders when real images are added."""
    _check_broker_token(token)
    if not get_property_images(property_id) and not _property_exists(property_id):
        raise HTTPException(status_code=404, detail="Property not found")
    uploaded = []
    for file in files[:10]:  # max 10 at once
        ct = file.content_type or "image/jpeg"
        if ct not in ALLOWED_IMAGE_TYPES:
            continue
        data = await file.read()
        if len(data) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
            continue
        url = upload_property_image(property_id, _safe_image_filename(file.filename, ct), data, ct)
        if url:
            uploaded.append(url)
    if uploaded:
        replace_unsplash_images(property_id, uploaded)
    return {"ok": True, "uploaded": len(uploaded), "urls": uploaded}


class DeleteImageReq(BaseModel):
    token: str
    image_url: str


@app.delete("/properties/{property_id}/images")
async def delete_image(property_id: str, req: DeleteImageReq):
    _check_broker_token(req.token)
    ok = delete_property_image(property_id, req.image_url)
    return {"ok": ok}


class ReorderImagesReq(BaseModel):
    token: str
    ordered_urls: list[str]


@app.post("/properties/{property_id}/images/reorder")
async def reorder_images(property_id: str, req: ReorderImagesReq):
    """Set the display order; first URL becomes the hero image."""
    _check_broker_token(req.token)
    ok = reorder_property_images(property_id, req.ordered_urls)
    return {"ok": ok}


def _safe_image_filename(filename: str | None, content_type: str) -> str:
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    stem = Path(filename or "photo").stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:60] or "photo"
    return f"{uuid.uuid4().hex[:12]}-{stem}.{ext}"


def _property_exists(property_id: str) -> bool:
    from database.supabase_client import get_client
    return bool(get_client().table("properties").select("id").eq("id", property_id).limit(1).execute().data)


@app.get("/broker/images/{property_id}", response_class=HTMLResponse)
async def broker_image_manager(property_id: str):
    pid = property_id
    content = f"""
<div style="margin-bottom:14px;font-size:13px;color:#64748b">
  <a href="/broker/edit/{pid}" style="color:#2563eb;font-weight:600">← Back to Edit</a> &nbsp;·&nbsp;
  Drag to reorder · First = hero image · Uploading real photos removes Unsplash placeholders
</div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
  <button class="btn btn-primary" onclick="document.getElementById('fp').click()">+ Upload Photos</button>
  <button class="btn btn-ghost" onclick="saveOrd()">Save Order</button>
  <button class="btn btn-ghost" onclick="loadImgs()">Refresh</button>
</div>
<input id="fp" type="file" accept="image/*" multiple style="display:none" onchange="upFiles()">
<div id="drop-zone" style="border:2px dashed #cbd5e1;border-radius:10px;padding:18px;text-align:center;color:#64748b;cursor:pointer;margin-bottom:14px" onclick="document.getElementById('fp').click()">
  Click or drag photos here (max 10 at once, 5MB each)
</div>
<div id="img-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px"></div>
<div id="img-st" style="margin-top:10px;font-size:13px;color:#059669"></div>
"""
    scripts = f"""<script>
const PID='{pid}';
async function loadImgs(){{
  const r=await fetch('/properties/'+PID+'/images');const d=await r.json();
  render(d.images||[]);
}}
function render(urls){{
  const g=document.getElementById('img-grid');g.innerHTML='';
  urls.forEach((u,i)=>{{
    const c=document.createElement('div');
    c.style.cssText='background:#fff;border:2px solid '+(i===0?'#2563eb':'#e2e8f0')+';border-radius:10px;overflow:hidden;position:relative;cursor:grab';
    c.draggable=true;c.dataset.url=u;
    c.innerHTML=(i===0?'<div style="position:absolute;top:6px;left:6px;background:#2563eb;color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px">HERO</div>':'')
      +'<img src="'+u+'" style="width:100%;height:110px;object-fit:cover" onerror="this.style.background=\'#e2e8f0\'">'
      +'<button onclick="delImg(\''+encodeURIComponent(u)+'\')" style="position:absolute;top:6px;right:6px;background:#ef4444;color:#fff;border:none;border-radius:50%;width:24px;height:24px;cursor:pointer;font-size:13px">×</button>';
    c.addEventListener('dragstart',e=>{{e.dataTransfer.setData('idx',i);c.style.opacity='.4';}});
    c.addEventListener('dragend',()=>c.style.opacity='1');
    c.addEventListener('dragover',e=>e.preventDefault());
    c.addEventListener('drop',e=>{{
      e.preventDefault();const from=+e.dataTransfer.getData('idx');
      const items=[...g.children];const mv=items[from];
      g.removeChild(mv);g.insertBefore(mv,items[i]);
      // Update hero badge
      [...g.children].forEach((el,j)=>{{
        el.style.borderColor=j===0?'#2563eb':'#e2e8f0';
        const hb=el.querySelector('div[style*="HERO"]');
        if(j===0&&!hb){{const nb=document.createElement('div');nb.innerHTML='HERO';nb.style.cssText='position:absolute;top:6px;left:6px;background:#2563eb;color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px';el.appendChild(nb);}}
        else if(j!==0&&hb)hb.remove();
      }});
    }});
    g.appendChild(c);
  }});
}}
async function saveOrd(){{
  const urls=[...document.querySelectorAll('#img-grid>div')].map(c=>c.dataset.url);
  await fetch('/properties/'+PID+'/images/reorder',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:tok(),ordered_urls:urls}})}});
  toast('Order saved');
}}
async function delImg(url){{
  if(!confirm('Delete this image?'))return;
  await fetch('/properties/'+PID+'/images',{{method:'DELETE',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:tok(),image_url:decodeURIComponent(url)}})}});
  loadImgs();
}}
async function upFiles(){{
  const files=document.getElementById('fp').files;if(!files.length)return;
  document.getElementById('img-st').textContent='Uploading '+files.length+' image(s)…';
  const fd=new FormData();fd.append('token',tok());
  for(const f of files)fd.append('files',f);
  const r=await fetch('/properties/'+PID+'/images/multi',{{method:'POST',body:fd}});
  const d=await r.json();
  document.getElementById('img-st').textContent=d.uploaded+' image(s) uploaded!';
  toast(d.uploaded+' photos uploaded');loadImgs();
}}
loadImgs();
</script>"""
    return _broker_page("Property Photos", content, active="LIST", scripts=scripts,
        hdr_extra=f'<a href="/broker/edit/{pid}" class="btn btn-ghost" style="font-size:12px;padding:7px 14px">← Edit Property</a>')


# ── Simple web chat interface ─────────────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"

@app.get("/static/shared.css")
async def shared_css():
    from fastapi.responses import Response
    css = (_TEMPLATES_DIR / "shared.css").read_text(encoding="utf-8")
    return Response(content=css, media_type="text/css")


@app.get("/", response_class=HTMLResponse)
async def web_chat():
    """Chat UI — new professional design."""
    tmpl = _TEMPLATES_DIR / "chat.html"
    if tmpl.exists():
        return HTMLResponse(tmpl.read_text(encoding="utf-8"))
    # fallback to old inline HTML if template missing
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Riya - Real Estate AI Lucknow</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f7fa;min-height:100vh}
#wrap{max-width:760px;margin:0 auto;display:flex;flex-direction:column;height:100vh;padding:0 12px}
#hdr{text-align:center;padding:10px 0 6px;display:flex;align-items:center;justify-content:center;gap:10px}
#hdr h1{font-size:20px;color:#1a73e8;font-weight:700}
#hdr p{font-size:12px;color:#888;margin-top:2px}
#hdr-left{flex:1}
#sl-btn{background:#fff;border:1.5px solid #1a73e8;color:#1a73e8;border-radius:20px;padding:6px 14px;font-size:13px;cursor:pointer;font-weight:600;white-space:nowrap}
#sl-btn:hover{background:#f0f4ff}
#sl-count{display:inline-block;background:#1a73e8;color:white;border-radius:50%;width:18px;height:18px;font-size:10px;text-align:center;line-height:18px;margin-left:4px;display:none}
#feed{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding:12px 0}
.bubble-row{display:flex;align-items:flex-end;gap:8px;scroll-margin-top:14px}
.bubble-row.user{flex-direction:row-reverse}
.avatar{width:32px;height:32px;border-radius:50%;background:#1a73e8;color:white;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0}
.bubble{max-width:78%;padding:10px 14px;border-radius:18px;line-height:1.55;font-size:14px;word-break:break-word}
.bubble.user{background:#1a73e8;color:white;border-bottom-right-radius:4px}
.bubble.riya{background:white;color:#1a1a2e;border-bottom-left-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.10)}
.typing{font-style:italic;color:#999;padding:10px 14px;background:white;border-radius:18px;border-bottom-left-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.10);font-size:14px;align-self:flex-start}
/* property cards */
.cards-block{width:100%;display:flex;flex-direction:column;gap:14px;margin-top:4px}
.pcard{background:white;border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,.10);overflow:hidden}
.pcard-gallery{position:relative;height:200px;background:#dde3ed;overflow:hidden}
.pcard-gallery img{width:100%;height:100%;object-fit:cover;display:block;cursor:pointer;transition:.2s}
.pcard-gallery img:hover{transform:scale(1.03)}
.gal-nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(0,0,0,.45);color:white;border:none;padding:6px 10px;cursor:pointer;font-size:16px;border-radius:6px;z-index:2}
.gal-nav.prev{left:6px}
.gal-nav.next{right:6px}
.gal-dots{position:absolute;bottom:8px;left:50%;transform:translateX(-50%);display:flex;gap:5px;z-index:2}
.gal-dot{width:7px;height:7px;border-radius:50%;background:rgba(255,255,255,.5);cursor:pointer}
.gal-dot.on{background:white}
.pcard-body{padding:14px 16px 16px}
.pcard-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px}
.pcard-title{font-size:15px;font-weight:700;color:#1a1a2e}
.pcard-price{font-size:16px;font-weight:800;color:#1a73e8;white-space:nowrap;margin-left:8px}
.pcard-sub{font-size:12px;color:#666;margin-bottom:10px}
.pcard-chips{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
.chip{background:#f0f4ff;color:#1a73e8;font-size:11px;padding:3px 9px;border-radius:20px;white-space:nowrap}
.pcard-conn{font-size:12px;color:#555;margin-bottom:12px;line-height:1.7}
.pcard-conn span{margin-right:10px}
.pcard-landmark{display:inline-block;font-size:12px;font-weight:600;color:#0a7d3c;background:#e6f7ee;border:1px solid #b6e6cb;border-radius:6px;padding:4px 9px;margin-bottom:10px}
.pcard-links{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.pcard-links a{font-size:12px;font-weight:600;color:#1d4ed8;text-decoration:none;background:#eef2ff;border:1px solid #c7d2fe;border-radius:6px;padding:4px 9px}
.pcard-actions{display:flex;gap:8px}
.btn-visit{flex:1;padding:10px;background:#1a73e8;color:white;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;transition:.15s}
.btn-visit:hover{background:#1558b0}
.btn-call{padding:10px 12px;background:#fff;color:#0f766e;border:1.5px solid #0f766e;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:.15s;white-space:nowrap}
.btn-call:hover{background:#ecfdf5}
.btn-save{padding:10px 14px;background:#fff;color:#e83030;border:1.5px solid #e83030;border-radius:10px;font-size:14px;cursor:pointer;transition:.15s;white-space:nowrap}
.btn-save:hover{background:#fff0f0}
.btn-save.saved{background:#e83030;color:white}
.btn-save.saved:hover{background:#c42020}
/* lightbox */
#lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:1000;align-items:center;justify-content:center;cursor:pointer}
#lb img{max-width:92vw;max-height:90vh;border-radius:10px}
/* shortlist panel */
#sl-panel{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:500;align-items:flex-start;justify-content:center;overflow-y:auto;padding:20px 12px}
#sl-box{background:#f5f7fa;border-radius:16px;max-width:720px;width:100%;padding:20px}
#sl-box h2{margin-bottom:14px;color:#1a1a2e;font-size:18px}
#sl-close{float:right;background:none;border:none;font-size:22px;cursor:pointer;color:#555}
#sl-cards{display:flex;flex-direction:column;gap:14px}
/* input */
#bar{display:flex;gap:8px;padding:10px 0 14px;align-items:center}
#inp{flex:1;padding:11px 18px;border-radius:24px;border:1.5px solid #ddd;font-size:14px;outline:none;background:white}
#inp:focus{border-color:#1a73e8;box-shadow:0 0 0 3px rgba(26,115,232,.13)}
#send-btn{padding:11px 22px;background:#1a73e8;color:white;border:none;border-radius:24px;cursor:pointer;font-size:14px;font-weight:700}
#send-btn:hover{background:#1558b0}
#send-btn:disabled{background:#aaa;cursor:default}
#mic-btn{padding:10px 14px;background:#fff;border:1.5px solid #ddd;border-radius:50%;cursor:pointer;font-size:18px;line-height:1;transition:.15s;flex-shrink:0}
#mic-btn:hover{border-color:#1a73e8;background:#f0f4ff}
#mic-btn.listening{background:#e83030;border-color:#e83030;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
@media(max-width:480px){.pcard-gallery{height:160px}.pcard-price{font-size:14px}}
</style>
</head>
<body>
<div id="lb" onclick="this.style.display='none'"><img id="lb-img" src="" alt=""/></div>

<!-- Shortlist panel -->
<div id="sl-panel" onclick="if(event.target===this)closeShortlist()">
  <div id="sl-box">
    <button id="sl-close" onclick="closeShortlist()">&#10005;</button>
    <h2>&#10084;&#65039; Saved Properties</h2>
    <div id="sl-cards"><p style="color:#888;font-size:14px">Loading...</p></div>
  </div>
</div>

<div id="wrap">
  <div id="hdr">
    <div id="hdr-left">
      <h1>Riya - Real Estate AI</h1>
      <p>Find your dream home in Lucknow &nbsp;·&nbsp; <a href="/properties/browse" style="color:#93c5fd;font-size:12px">Browse all properties</a></p>
    </div>
    <button id="sl-btn" onclick="openShortlist()">&#10084;&#65039; Saved <span id="sl-count">0</span></button>
  </div>
  <div id="feed"></div>
  <div id="bar">
    <input id="inp" placeholder="Type your message..." />
    <button id="mic-btn" title="Voice input">🎤</button>
    <button id="send-btn" onclick="send()">Send</button>
  </div>
</div>
<script>
// ── Session persistence via localStorage ──────────────────────────────────
let SID = localStorage.getItem('riya_sid');
if (!SID) {
  SID = 'web_' + Math.random().toString(36).slice(2, 11);
  localStorage.setItem('riya_sid', SID);
}

const feed    = document.getElementById('feed');
const inp     = document.getElementById('inp');
const sendBtn = document.getElementById('send-btn');
const slCount = document.getElementById('sl-count');

// Track saved property IDs client-side for instant UI feedback
const savedIds = new Set(JSON.parse(localStorage.getItem('riya_saved') || '[]'));
updateSlCount();

inp.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) send(); });

// ── Web Speech API: voice in (mic) + voice out (Riya speaks) — 100% free ────
let voiceReply = false;   // speak Riya's next reply if the user spoke
function speak(text) {
  try {
    if (!window.speechSynthesis) return;
    // strip markdown/links/emoji so the spoken version is clean
    let t = text.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
                .replace(/https?:\/\/\S+/g, '')
                .replace(/[*_#`]/g, '')
                .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, '');
    const u = new SpeechSynthesisUtterance(t);
    u.lang = 'en-IN'; u.rate = 1.02; u.pitch = 1.05;
    const vs = window.speechSynthesis.getVoices();
    const pick = vs.find(v => /female|Google UK English Female|en-IN/i.test(v.name + v.lang)) || vs.find(v => v.lang === 'en-IN');
    if (pick) u.voice = pick;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  } catch (e) {}
}
const micBtn = document.getElementById('mic-btn');
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR) {
  const rec = new SR();
  rec.lang = 'en-IN';
  rec.interimResults = false;
  rec.maxAlternatives = 1;
  rec.onresult = e => {
    const transcript = e.results[0][0].transcript;
    inp.value = transcript;
    voiceReply = true;   // user spoke → speak the reply back
    micBtn.textContent = '🎤';
    micBtn.classList.remove('listening');
    micBtn.disabled = false;
    // Auto-send after a short pause so user can see the transcript
    setTimeout(() => { if (inp.value.trim()) send(); }, 400);
  };
  rec.onerror = () => { micBtn.textContent = '🎤'; micBtn.classList.remove('listening'); micBtn.disabled = false; };
  rec.onend = () => { micBtn.textContent = '🎤'; micBtn.classList.remove('listening'); micBtn.disabled = false; };
  micBtn.onclick = () => {
    micBtn.textContent = '🔴';
    micBtn.classList.add('listening');
    micBtn.disabled = true;
    rec.start();
  };
} else {
  micBtn.style.display = 'none'; // browser doesn't support Web Speech API
}

// ── Auto-init: send a silent "hi" to start onboarding on page load ──────
window.addEventListener('load', () => {
  sendSilent('__init__');
});

async function sendSilent(msg) {
  const typing = addTyping();
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SID, message: msg, platform: 'web' }),
    });
    const data = await res.json();
    typing.remove();
    addBubble(data.reply, 'riya');
    if (data.properties && data.properties.length > 0) addCards(data.properties);
  } catch (e) {
    typing.textContent = 'Connection error. Please refresh.';
    typing.className = 'bubble riya';
  }
}

async function send() {
  const txt = inp.value.trim();
  if (!txt || sendBtn.disabled) return;
  inp.value = '';
  sendBtn.disabled = true;
  addBubble(txt, 'user');
  const typing = addTyping();
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SID, message: txt, platform: 'web' }),
    });
    const data = await res.json();
    typing.remove();
    const riyaRow = addBubble(data.reply, 'riya');
    if (data.properties && data.properties.length > 0) addCards(data.properties);
    if (voiceReply) { speak(data.reply); voiceReply = false; }   // talk back if they spoke
    // Land the view on the START of Riya's reply so the user reads top-to-bottom,
    // instead of being thrown to the very bottom of the property cards.
    requestAnimationFrame(() => riyaRow.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  } catch (e) {
    typing.textContent = 'Connection error. Try again.';
    typing.className = 'bubble riya';
  } finally {
    sendBtn.disabled = false;
    inp.focus();
  }
}

function addBubble(text, who) {
  const row = document.createElement('div');
  row.className = 'bubble-row' + (who === 'user' ? ' user' : '');
  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = who === 'user' ? 'U' : 'R';
  if (who === 'user') av.style.background = '#555';
  const bbl = document.createElement('div');
  bbl.className = 'bubble ' + who;
  bbl.innerHTML = mdToHtml(text);
  if (who === 'user') { row.appendChild(bbl); row.appendChild(av); }
  else { row.appendChild(av); row.appendChild(bbl); }
  feed.appendChild(row);
  // User messages scroll to bottom (show what they just sent). Riya replies are
  // positioned explicitly by the caller so the reader sees the START of the reply.
  if (who === 'user') feed.scrollTop = feed.scrollHeight;
  return row;
}

function addTyping() {
  const el = document.createElement('div');
  el.className = 'typing';
  el.textContent = 'Riya is typing...';
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
  return el;
}

function mdToHtml(t) {
  return t
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/_(.+?)_/g, '<em>$1</em>')
    // [label](url) → clean clickable link
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // bare http(s) URLs → short clickable link (don't dump the whole URL)
    .replace(/(^|[^"'>])(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">link</a>')
    .replace(/\n/g, '<br>');
}

function addCards(props) {
  const block = document.createElement('div');
  block.className = 'cards-block';
  props.forEach((p, idx) => block.appendChild(buildCard(p, idx + 1)));
  feed.appendChild(block);
  // No forced scroll here — send() positions the view at the start of Riya's reply.
}

function buildCard(p, num) {
  const card = document.createElement('div');
  card.className = 'pcard';

  // Gallery
  const imgs = p.images || [];
  const gal = document.createElement('div');
  gal.className = 'pcard-gallery';
  if (imgs.length > 0) {
    let cur = 0;
    const imgEl = document.createElement('img');
    imgEl.src = imgs[0];
    imgEl.alt = 'Property photo';
    imgEl.onerror = () => { imgEl.style.display = 'none'; };
    imgEl.onclick = () => { document.getElementById('lb-img').src = imgEl.src; document.getElementById('lb').style.display = 'flex'; };
    gal.appendChild(imgEl);

    if (imgs.length > 1) {
      const dots = document.createElement('div'); dots.className = 'gal-dots';
      imgs.forEach((_, i) => {
        const d = document.createElement('div');
        d.className = 'gal-dot' + (i === 0 ? ' on' : '');
        dots.appendChild(d);
      });
      gal.appendChild(dots);

      const prevBtn = document.createElement('button'); prevBtn.className = 'gal-nav prev'; prevBtn.textContent = '<';
      const nextBtn = document.createElement('button'); nextBtn.className = 'gal-nav next'; nextBtn.textContent = '>';
      const allDots = dots.querySelectorAll('.gal-dot');
      function goTo(i) {
        cur = i; imgEl.src = imgs[cur];
        allDots.forEach((d, j) => d.classList.toggle('on', j === cur));
      }
      prevBtn.onclick = e => { e.stopPropagation(); goTo((cur - 1 + imgs.length) % imgs.length); };
      nextBtn.onclick = e => { e.stopPropagation(); goTo((cur + 1) % imgs.length); };
      allDots.forEach((d, i) => d.onclick = () => goTo(i));
      gal.appendChild(prevBtn); gal.appendChild(nextBtn);
    }
  }
  card.appendChild(gal);

  // Body
  const body = document.createElement('div');
  body.className = 'pcard-body';

  const top = document.createElement('div'); top.className = 'pcard-top';
  const title = document.createElement('div'); title.className = 'pcard-title';
  title.textContent = num + '. ' + (p.bhk || '') + ' BHK ' + (p.property_type || '');
  const price = document.createElement('div'); price.className = 'pcard-price';
  price.textContent = (p.price_str || '').replace('Rs.', 'Rs.​');
  top.appendChild(title); top.appendChild(price);
  body.appendChild(top);

  const sub = document.createElement('div'); sub.className = 'pcard-sub';
  const parts = [];
  if (p.area) parts.push('📍 ' + p.area);
  if (p.sqft) parts.push(p.sqft + ' sqft');
  if (p.furnishing) parts.push(p.furnishing);
  if (p.floor) parts.push('Floor ' + p.floor);
  sub.textContent = parts.join('  |  ');
  body.appendChild(sub);

  // Live distance to a buyer-named landmark (computed this search)
  if (p.landmark_name && (p.landmark_distance_km || p.landmark_distance_km === 0)) {
    const lm = document.createElement('div');
    lm.className = 'pcard-landmark';
    lm.textContent = '🎯 ' + p.landmark_distance_km + ' km from ' + p.landmark_name;
    body.appendChild(lm);
  }

  if (p.top_amenities && p.top_amenities.length > 0) {
    const chips = document.createElement('div'); chips.className = 'pcard-chips';
    p.top_amenities.slice(0, 8).forEach(a => {
      const c = document.createElement('span'); c.className = 'chip'; c.textContent = a; chips.appendChild(c);
    });
    body.appendChild(chips);
  }

  if (p.connectivity && Object.keys(p.connectivity).length > 0) {
    const conn = document.createElement('div'); conn.className = 'pcard-conn';
    Object.entries(p.connectivity).slice(0, 4).forEach(([k, v]) => {
      const s = document.createElement('span'); s.textContent = '📍' + k + ' ' + v; conn.appendChild(s);
    });
    body.appendChild(conn);
  }

  // Map + documents (floor plan / brochure / papers uploaded by the broker)
  const links = document.createElement('div'); links.className = 'pcard-links';
  if (p.map_url) {
    const a = document.createElement('a'); a.href = p.map_url; a.target = '_blank';
    a.textContent = '🗺️ View on map'; links.appendChild(a);
  }
  if (p.documents && p.documents.length) {
    p.documents.forEach(d => {
      const a = document.createElement('a'); a.href = d.url; a.target = '_blank';
      a.textContent = '📄 ' + (d.label || 'Document'); links.appendChild(a);
    });
  }
  if (links.children.length) body.appendChild(links);

  // Action buttons: Visit + Save
  const actions = document.createElement('div'); actions.className = 'pcard-actions';

  const visitBtn = document.createElement('button');
  visitBtn.className = 'btn-visit';
  visitBtn.textContent = 'Book Site Visit';
  visitBtn.onclick = () => { inp.value = 'I want to visit property ' + num; send(); };

  const callBtn = document.createElement('button');
  callBtn.className = 'btn-call';
  callBtn.textContent = '📞 Request callback';
  callBtn.onclick = () => { inp.value = 'Please have someone call me back about property ' + num; send(); };

  const isSaved = savedIds.has(p.id);
  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn-save' + (isSaved ? ' saved' : '');
  saveBtn.textContent = isSaved ? '❤️ Saved' : '🤍 Save';
  saveBtn.dataset.id = p.id;
  saveBtn.onclick = () => toggleSave(p.id, saveBtn);

  actions.appendChild(visitBtn);
  actions.appendChild(callBtn);
  actions.appendChild(saveBtn);
  body.appendChild(actions);
  card.appendChild(body);
  return card;
}

async function toggleSave(propertyId, btn) {
  const alreadySaved = savedIds.has(propertyId);
  btn.disabled = true;
  try {
    const method = alreadySaved ? 'DELETE' : 'POST';
    const res = await fetch('/shortlist', {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SID, property_id: propertyId }),
    });
    const data = await res.json();
    if (data.ok) {
      if (alreadySaved) {
        savedIds.delete(propertyId);
        btn.textContent = '🤍 Save';
        btn.classList.remove('saved');
      } else {
        savedIds.add(propertyId);
        btn.textContent = '❤️ Saved';
        btn.classList.add('saved');
      }
      localStorage.setItem('riya_saved', JSON.stringify([...savedIds]));
      updateSlCount();
    }
  } catch (e) {
    console.error('Save error:', e);
  } finally {
    btn.disabled = false;
  }
}

function updateSlCount() {
  const n = savedIds.size;
  slCount.textContent = n;
  slCount.style.display = n > 0 ? 'inline-block' : 'none';
}

async function openShortlist() {
  document.getElementById('sl-panel').style.display = 'flex';
  const container = document.getElementById('sl-cards');
  container.innerHTML = '<p style="color:#888;font-size:14px">Loading...</p>';
  try {
    const res = await fetch('/shortlist/' + SID);
    const data = await res.json();
    container.innerHTML = '';
    if (!data.properties || data.properties.length === 0) {
      container.innerHTML = '<p style="color:#888;font-size:14px">No saved properties yet. Click "🤍 Save" on any property card!</p>';
      return;
    }
    data.properties.forEach((p, i) => container.appendChild(buildCard(p, i + 1)));
  } catch (e) {
    container.innerHTML = '<p style="color:#c00;font-size:14px">Could not load saved properties.</p>';
  }
}

function closeShortlist() {
  document.getElementById('sl-panel').style.display = 'none';
}
</script>
</body>
</html>"""


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Public property browse page ───────────────────────────────────────────────

def _broker_page(title: str, content: str, active: str = "", hdr_extra: str = "", scripts: str = "") -> str:
    """Render a broker page using the shared sidebar shell template."""
    tmpl = (_TEMPLATES_DIR / "broker_base.html").read_text(encoding="utf-8")
    flags = {"DASH":"","PIPE":"","ANAL":"","MEET":"","LIST":"","ADD":"","UPL":""}
    if active in flags:
        flags[active] = "active"
    for k, v in flags.items():
        tmpl = tmpl.replace("{{A_" + k + "}}", v)
    return (tmpl
        .replace("{{TITLE}}", title)
        .replace("{{CONTENT}}", content)
        .replace("{{HDR_EXTRA}}", hdr_extra)
        .replace("{{SCRIPTS}}", scripts))


@app.get("/properties/browse", response_class=HTMLResponse)
async def properties_browse():
    tmpl = _TEMPLATES_DIR / "browse.html"
    if tmpl.exists():
        return HTMLResponse(tmpl.read_text(encoding="utf-8"))
    return _BROWSE_HTML


@app.get("/api/properties/browse")
async def api_browse(area: str = "", bhk: int = None, min_price: int = None,
                     max_price: int = None, prop_type: str = "", limit: int = 30):
    """Public endpoint for the browse page — no auth, direct DB query."""
    from database.supabase_client import get_client
    if min_price is not None and min_price < 0:
        raise HTTPException(status_code=400, detail="Minimum price cannot be negative")
    if max_price is not None and max_price < 0:
        raise HTTPException(status_code=400, detail="Maximum price cannot be negative")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(status_code=400, detail="Minimum price cannot exceed maximum price")
    if bhk is not None and not 1 <= bhk <= 20:
        raise HTTPException(status_code=400, detail="BHK must be between 1 and 20")
    limit = max(1, min(limit, 100))
    cl = get_client()
    q = cl.table("properties").select(
        "id,area_name,city,bhk,price_inr,property_type,status,data"
    ).eq("status", "available").order("price_inr").limit(limit)
    if bhk:
        q = q.eq("bhk", bhk)
    if prop_type:
        q = q.ilike("property_type", f"%{prop_type}%")
    if max_price:
        q = q.lte("price_inr", max_price)
    if min_price:
        q = q.gte("price_inr", min_price)
    if area:
        q = q.ilike("area_name", f"%{area.strip()}%")
    rows = q.execute().data or []

    def fmt(p):
        cr = p / 1e7
        return f"Rs.{cr:.2f} Cr" if cr >= 1 else f"Rs.{p/1e5:.0f} L"

    out = []
    for r in rows:
        d = r.get("data") or {}
        imgs = d.get("images") or []
        conn = d.get("connectivity") or {}
        amen = d.get("amenities") or []
        prof = d.get("property_profile") or {}
        out.append({
            "id": r["id"], "area": r.get("area_name"), "city": r.get("city"),
            "bhk": r.get("bhk"), "price_inr": r.get("price_inr"),
            "price_str": fmt(r["price_inr"]) if r.get("price_inr") else "POA",
            "prop_type": r.get("property_type", ""),
            "sqft": prof.get("builtup_area_sqft"),
            "furnishing": prof.get("furnishing"),
            "hero_img": imgs[0] if imgs else "",
            "metro": conn.get("metro_distance_km"),
            "amenities": amen[:4],
        })
    return {"properties": out, "count": len(out)}


# ── Broker dashboard ─────────────────────────────────────────────────────────
# Lightweight, free: JSON endpoints + one static HTML page. Protected by a shared
# token (set BROKER_TOKEN in .env; defaults to "broker" for local use).

def _broker_token() -> str:
    return os.environ.get("BROKER_TOKEN", "")


def _check_broker_token(token: str | None):
    configured = _broker_token()
    if not configured:
        raise HTTPException(status_code=503, detail="BROKER_TOKEN is not configured")
    if not token or not hmac.compare_digest(token, configured):
        raise HTTPException(status_code=401, detail="Invalid broker token")


@app.get("/broker/pipeline", response_class=HTMLResponse)
async def broker_pipeline_page():
    stages = [
        ("new","New","#2563eb"),("contacted","Contacted","#0891b2"),
        ("visit","Visit Scheduled","#7c3aed"),("met","Site Visited","#d97706"),
        ("negotiating","Negotiating","#ea580c"),("won","Won ✓","#16a34a"),
        ("waiting","On Hold","#64748b"),("lost","Not Interested","#dc2626"),
    ]
    cols = "".join(f'<div class="kcol" data-stage="{s}" style="--col-color:{c}"><div class="kcol-hdr"><span>{lbl}</span><span class="kcnt" id="cnt-{s}">0</span></div><div class="kcards" id="col-{s}"></div></div>' for s,lbl,c in stages)
    content = f'<div class="kboard">{cols}</div>'
    scripts = """<script>
const STAGES=["new","contacted","visit","met","negotiating","won","waiting","lost"];
let _leads=[];
async function loadPipe(){
  const t=tok();if(!t){document.querySelector('.kboard').innerHTML='<p style="color:#94a3b8;padding:20px">Enter broker token in the sidebar.</p>';return;}
  const r=await fetch('/broker/leads?token='+encodeURIComponent(t)+'&status=all');
  const d=await r.json(); _leads=d.leads||[];
  render();
}
function render(){
  STAGES.forEach(s=>{
    document.getElementById('col-'+s).innerHTML='';
    document.getElementById('cnt-'+s).textContent=_leads.filter(l=>l.status===s).length;
  });
  _leads.forEach(l=>document.getElementById('col-'+(l.status||'new')).appendChild(makeCard(l)));
}
function makeCard(l){
  const c=document.createElement('div');c.className='klcard';c.draggable=true;
  const phone=(l.phone||'').replace(/[^0-9]/g,'');
  c.innerHTML=`<div class="kl-name">${l.name||'Unknown'}</div>
    <div class="kl-phone">${l.phone||''}</div>
    <div class="kl-meta">${l.preferred_area||''} ${l.budget_max?'· ₹'+(l.budget_max/1e7).toFixed(1)+'Cr':''}</div>
    <div class="kl-btns">
      <a href="https://wa.me/91${phone}" target="_blank" class="kl-wa">WhatsApp</a>
      ${phone?`<a href="tel:${l.phone}" class="kl-call">Call</a>`:''}
      <button class="kl-note" onclick="addNote('${l.id}','${(l.status||'new')}')">Note</button>
    </div>`;
  c.addEventListener('dragstart',e=>{e.dataTransfer.setData('lid',l.id);c.style.opacity='.4';});
  c.addEventListener('dragend',()=>c.style.opacity='1');
  return c;
}
document.querySelectorAll('.kcards').forEach(col=>{
  col.addEventListener('dragover',e=>e.preventDefault());
  col.addEventListener('drop',async e=>{
    e.preventDefault();
    const id=e.dataTransfer.getData('lid');
    const stage=col.closest('.kcol').dataset.stage;
    await fetch('/broker/leads/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok(),status:stage})});
    const lead=_leads.find(l=>l.id===id);if(lead)lead.status=stage;
    render();
    if(stage==='won'){const p=lead?.interested_property_id;if(p&&confirm('Mark property sold?'))await fetch('/broker/properties/'+p+'/sold',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok()})});}
  });
});
async function addNote(id,status){const txt=prompt('Add note:');if(!txt)return;await fetch('/broker/leads/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok(),status,notes:txt})});loadPipe();}
loadPipe();
</script>
<style>
.kboard{display:flex;gap:10px;overflow-x:auto;padding-bottom:16px;min-height:70vh}
.kcol{flex:0 0 210px;background:#f8fafc;border-radius:10px;border:1px solid #e2e8f0;display:flex;flex-direction:column}
.kcol-hdr{padding:10px 12px;font-size:12px;font-weight:700;color:#334155;
  border-bottom:3px solid var(--col-color,#2563eb);display:flex;justify-content:space-between}
.kcnt{background:#e2e8f0;border-radius:99px;padding:1px 8px;font-size:11px}
.kcards{padding:8px;flex:1;min-height:80px}
.klcard{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;
  margin-bottom:8px;cursor:grab;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.klcard:active{cursor:grabbing}
.kl-name{font-weight:700;font-size:13px;color:#0f172a}
.kl-phone{font-size:12px;color:#059669;font-weight:600}
.kl-meta{font-size:11px;color:#64748b;margin:3px 0 7px}
.kl-btns{display:flex;gap:5px;flex-wrap:wrap}
.kl-btns a,.kl-btns button{font-size:11px;padding:3px 8px;border-radius:6px;cursor:pointer;
  border:1px solid #e2e8f0;background:#f8fafc;color:#334155;text-decoration:none}
.kl-wa{background:#dcfce7!important;border-color:#86efac!important;color:#166534!important}
.kl-call{background:#eff6ff!important;border-color:#93c5fd!important;color:#1d4ed8!important}
</style>"""
    return _broker_page("Lead Pipeline", content, active="PIPE", scripts=scripts)


@app.get("/broker/leads")
async def broker_leads(token: str, status: str = "all"):
    """All leads for the dashboard (newest first). status: all|new|contacted|visit|converted."""
    _check_broker_token(token)
    try:
        return {"leads": get_all_leads(status=status)}
    except Exception as e:
        logger.error(f"broker_leads error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class LeadStatusUpdate(BaseModel):
    token: str
    status: str
    notes: str | None = None


PIPELINE_STAGES = {"new", "contacted", "visit", "met", "negotiating", "won", "waiting", "lost"}


@app.post("/broker/leads/{lead_id}/status")
async def broker_update_lead(lead_id: str, req: LeadStatusUpdate):
    _check_broker_token(req.token)
    if req.status not in PIPELINE_STAGES:
        raise HTTPException(status_code=400, detail="Invalid pipeline stage")
    try:
        update_lead_status(lead_id, req.status, req.notes)
        return {"ok": True}
    except Exception as e:
        logger.error(f"broker_update_lead error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class PropertySold(BaseModel):
    token: str


@app.post("/broker/properties/{property_id}/sold")
async def broker_mark_sold(property_id: str, req: PropertySold):
    """Mark a property booked/sold so the bot stops recommending it."""
    _check_broker_token(req.token)
    try:
        if not mark_property_sold(property_id):
            raise HTTPException(status_code=404, detail="Property not found")
        return {"ok": True}
    except Exception as e:
        logger.error(f"broker_mark_sold error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/broker", response_class=HTMLResponse)
async def broker_dashboard():
    content = """
<div class="grid-3" id="stats"></div>
<div style="margin-top:20px">
  <h2 style="font-size:15px;font-weight:700;color:#334155;margin-bottom:12px">Recent Leads</h2>
  <div id="leads-wrap"></div>
</div>
"""
    scripts = """<script>
async function loadDash(){
  const t=tok(); if(!t){document.getElementById('stats').innerHTML='<p style="color:#94a3b8;font-size:14px;grid-column:1/-1">Enter your broker token in the sidebar to load data.</p>';return;}
  const [aRes, lRes] = await Promise.all([
    fetch('/broker/analytics?token='+encodeURIComponent(t)),
    fetch('/broker/leads?token='+encodeURIComponent(t)+'&status=all')
  ]);
  if(!aRes.ok){document.getElementById('stats').innerHTML='<p style="color:#dc2626;grid-column:1/-1">Invalid token.</p>';return;}
  const a=await aRes.json(), l=await lRes.json();
  const leads=l.leads||[];
  document.getElementById('stats').innerHTML=`
    <div class="stat"><div class="stat-val">${a.leads_total||0}</div><div class="stat-lbl">Total Leads</div></div>
    <div class="stat"><div class="stat-val">${a.leads_this_week||0}</div><div class="stat-lbl">This Week</div></div>
    <div class="stat"><div class="stat-val" style="color:#059669">${a.conversion_rate||0}%</div><div class="stat-lbl">Conversion</div></div>
    <div class="stat"><div class="stat-val">${a.properties_total||0}</div><div class="stat-lbl">Properties</div></div>
    <div class="stat"><div class="stat-val" style="color:#059669">${a.properties_available||0}</div><div class="stat-lbl">Available</div></div>
    <div class="stat"><div class="stat-val" style="color:#dc2626">${a.properties_sold||0}</div><div class="stat-lbl">Sold/Booked</div></div>
  `;
  const STATUS_COLOR={'new':'badge-new','contacted':'badge-new','visit':'badge-visit','won':'badge-won','lost':'badge-lost','waiting':'badge-wait','met':'badge-visit','negotiating':'badge-amber'};
  const rows=leads.slice(0,8).map(l=>`<tr>
    <td><b>${l.name||'—'}</b></td>
    <td><a href="tel:${l.phone||''}" style="color:#059669;font-weight:600">${l.phone||'—'}</a></td>
    <td>${l.preferred_area||'—'}</td>
    <td>${l.budget_max?fmt(l.budget_max):'—'}</td>
    <td><span class="badge ${STATUS_COLOR[l.status]||'badge-wait'}">${l.status||'new'}</span></td>
    <td><a href="https://wa.me/91${(l.phone||'').replace(/[^0-9]/g,'')}" target="_blank" style="color:#059669;font-weight:600;text-decoration:none">WhatsApp</a></td>
  </tr>`).join('');
  document.getElementById('leads-wrap').innerHTML=leads.length?
    '<div class="card"><table class="table"><thead><tr><th>Name</th><th>Phone</th><th>Area</th><th>Budget</th><th>Status</th><th></th></tr></thead><tbody>'+rows+'</tbody></table></div>'
    +'<p style="text-align:center;margin-top:10px"><a href="/broker/pipeline" style="color:#2563eb;font-size:13px;font-weight:600">View all leads in pipeline →</a></p>'
    :'<p style="color:#94a3b8;font-size:14px">No leads yet.</p>';
}
loadDash();
</script>"""
    return _broker_page("Dashboard", content, active="DASH",
        hdr_extra='<a href="/broker/pipeline" class="btn btn-primary" style="font-size:12px;padding:7px 14px">🎯 Open Pipeline</a>',
        scripts=scripts)


# ── Broker: add a single property + upload images & documents ────────────────

class NewProperty(BaseModel):
    token: str
    property_type: str
    bhk: int | None = None
    price_inr: int
    area_sqft: float | None = None
    furnishing: str | None = None
    address: str
    city: str = "Lucknow"
    amenities: str | None = None          # comma-separated
    broker_name: str | None = None
    broker_phone: str | None = None
    description: str | None = None


@app.post("/broker/property")
async def broker_add_property(req: NewProperty):
    """Add ONE property via the broker form (normalize → geocode → enrich → embed → store)."""
    _check_broker_token(req.token)
    fields = req.model_dump(exclude={"token"})
    result = create_property_from_fields(fields, broker_id=req.broker_name or "broker_ui")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Could not add property"))
    return result


@app.post("/broker/properties/{property_id}/documents")
async def broker_upload_document(property_id: str, token: str = Form(...),
                                 label: str = Form("Document"), file: UploadFile = File(...)):
    """Attach a document (PDF/image) — floor plan, brochure, papers — to a property."""
    _check_broker_token(token)
    ct = file.content_type or "application/pdf"
    allowed = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
    if ct not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported document type: {ct}. Use PDF/JPEG/PNG.")
    file_bytes = await file.read()
    if len(file_bytes) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Document too large — max 15 MB")
    url = upload_property_document(property_id, file.filename or "document", file_bytes, ct)
    if not url:
        raise HTTPException(status_code=500, detail="Upload failed — check 'property-documents' Storage bucket exists")
    add_document_to_property(property_id, url, label)
    return {"ok": True, "property_id": property_id, "document_url": url, "label": label}


@app.get("/broker/properties/{property_id}/documents")
async def broker_list_documents(property_id: str):
    return {"property_id": property_id, "documents": get_property_documents(property_id)}


@app.get("/broker/add", response_class=HTMLResponse)
async def broker_add_page():
    # Inline the add-property form inside the sidebar shell
    content = """
<div class="page-sm" style="padding:0">
<div class="card card-body">
<div class="row"><div><label class="field-label">Property Type *</label>
<select id="f-type" class="input"><option>Flat</option><option>Independent House</option><option>Villa</option><option>Builder Floor</option><option>Plot</option><option>Shop</option><option>Office</option></select></div>
<div><label class="field-label">BHK *</label><input id="f-bhk" class="input" type="number" min="1" max="10" placeholder="3"></div></div>
<div class="row"><div><label class="field-label">Price (₹) *</label><input id="f-price" class="input" type="number" placeholder="8500000"></div>
<div><label class="field-label">Area (sqft)</label><input id="f-sqft" class="input" type="number" placeholder="1200"></div></div>
<label class="field-label">Area / Locality *</label><input id="f-area" class="input" placeholder="Gomti Nagar">
<label class="field-label">Full Address</label><input id="f-addr" class="input" placeholder="Plot 42, Sector B, Gomti Nagar">
<label class="field-label">City</label><input id="f-city" class="input" value="Lucknow">
<div class="row"><div><label class="field-label">Furnishing</label>
<select id="f-furn" class="input"><option>Furnished</option><option>Semi-Furnished</option><option>Unfurnished</option></select></div>
<div><label class="field-label">Transaction</label>
<select id="f-txn" class="input"><option>New</option><option>Resale</option></select></div></div>
<label class="field-label">Amenities (comma-separated)</label>
<input id="f-amen" class="input" placeholder="Lift, Gym, Pool, Parking">
<label class="field-label">Broker Name</label><input id="f-bname" class="input" placeholder="Your name">
<label class="field-label">Broker Phone</label><input id="f-bphone" class="input" placeholder="9876543210">
<label class="field-label">Description</label>
<textarea id="f-desc" class="input" style="height:80px" placeholder="Describe the property..."></textarea>
<div style="margin-top:16px;display:flex;gap:10px">
<button class="btn btn-primary" onclick="addProp()">Add Property</button>
</div>
<div id="result" style="margin-top:10px;font-size:13px"></div>
</div></div>"""
    scripts = """<script>
async function addProp(){
  const t=tok();if(!t){toast('Enter broker token in sidebar','false');return;}
  const amen=document.getElementById('f-amen').value.split(',').map(s=>s.trim()).filter(Boolean);
  const body={token:t,
    property_type:document.getElementById('f-type').value,
    bhk:+document.getElementById('f-bhk').value||null,
    price_inr:+document.getElementById('f-price').value||null,
    area_sqft:+document.getElementById('f-sqft').value||null,
    area_name:document.getElementById('f-area').value,
    address:document.getElementById('f-addr').value,
    city:document.getElementById('f-city').value||'Lucknow',
    furnishing:document.getElementById('f-furn').value,
    transaction_type:document.getElementById('f-txn').value,
    amenities:amen,
    broker_name:document.getElementById('f-bname').value,
    broker_phone:document.getElementById('f-bphone').value,
    description:document.getElementById('f-desc').value,
  };
  if(!body.price_inr||!body.area_name){toast('Price and Area are required','false');return;}
  document.getElementById('result').innerHTML='<span style="color:#0891b2">Adding property + geocoding (may take 5-10s)…</span>';
  const r=await fetch('/broker/property',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.id||d.property_id){
    const pid=d.id||d.property_id;
    document.getElementById('result').innerHTML='<span style="color:#059669">✓ Property added! <a href="/broker/edit/'+pid+'">Edit details</a> · <a href="/broker/images/'+pid+'">Upload photos</a></span>';
    toast('Property added successfully');
  } else {
    document.getElementById('result').innerHTML='<span style="color:#dc2626">Error: '+(d.detail||JSON.stringify(d))+'</span>';
  }
}
</script>"""
    return _broker_page("Add Property", content, active="ADD", scripts=scripts,
        hdr_extra='<a href="/broker/listings" class="btn btn-ghost" style="font-size:12px;padding:7px 14px">My Listings</a>')


@app.get("/broker/upload", response_class=HTMLResponse)
async def broker_upload_page():
    content = """
<div class="card card-body" style="max-width:680px">
<div style="background:#fef9c3;border:1px solid #fde68a;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:14px">
💡 Any column names work — Riya auto-detects them. Review the mapping before importing.
Each property will be geocoded (lat/lng) + enriched with nearby metro/hospital/school distances.
</div>
<label class="field-label">CSV File</label>
<div id="dropzone" style="border:2px dashed #cbd5e1;border-radius:10px;padding:24px;text-align:center;color:#64748b;cursor:pointer;margin-bottom:14px" onclick="document.getElementById('csv').click()">
  Click to choose CSV file<br><span style="font-size:12px" id="fname"></span>
</div>
<input id="csv" type="file" accept=".csv" style="display:none" onchange="preview()">
<div id="preview-section" style="display:none">
  <h3 style="font-size:14px;font-weight:700;margin-bottom:8px">Column Mapping <span id="map-note" style="font-size:12px;color:#64748b;font-weight:400"></span></h3>
  <table class="table" id="map-table" style="margin-bottom:12px"></table>
  <div id="sample-wrap"></div>
  <button class="btn btn-primary" style="margin-top:12px" onclick="doImport()">Import All Rows</button>
  <div id="imp-result" style="margin-top:10px;font-size:13px"></div>
</div>
</div>"""
    scripts = """<script>
let _file=null,_headers=[],_mapping={};
async function preview(){
  _file=document.getElementById('csv').files[0];if(!_file)return;
  document.getElementById('fname').textContent=_file.name;
  const fd=new FormData();fd.append('token',tok()||'broker');fd.append('file',_file);
  const r=await fetch('/upload/preview',{method:'POST',body:fd});
  if(!r.ok){toast('Preview failed: '+await r.text(),'false');return;}
  const d=await r.json();_headers=d.headers||[];_mapping=d.column_mapping||{};
  document.getElementById('preview-section').style.display='block';
  document.getElementById('map-note').textContent=d.total_rows+' rows · '+_headers.length+' columns';
  const can=['property_type','bhk','price_inr','area_sqft','address','city','furnishing','amenities','broker_name','broker_phone','description'];
  const lbl={'property_type':'Type *','bhk':'BHK','price_inr':'Price ₹ *','area_sqft':'Area sqft','address':'Address *','city':'City','furnishing':'Furnishing','amenities':'Amenities','broker_name':'Broker','broker_phone':'Broker Phone','description':'Description'};
  document.getElementById('map-table').innerHTML='<tr><th>Field</th><th>CSV Column</th></tr>'
    +can.map(c=>'<tr><td>'+lbl[c]+'</td><td><select class="input map-sel" id="m_'+c+'" style="font-size:12px;padding:4px"><option value="">— skip —</option>'+_headers.map(h=>'<option'+(h===_mapping[c]?' selected':'')+'>'+h+'</option>').join('')+'</select></td></tr>').join('');
  if(d.sample_rows?.length){
    document.getElementById('sample-wrap').innerHTML='<p style="font-size:12px;font-weight:600;margin-bottom:6px">Sample rows:</p><div style="overflow-x:auto"><table class="table"><tr>'+_headers.map(h=>'<th>'+h+'</th>').join('')+'</tr>'+d.sample_rows.map(row=>'<tr>'+_headers.map(h=>'<td>'+(row[h]||'')+'</td>').join('')+'</tr>').join('')+'</table></div>';
  }
}
async function doImport(){
  if(!_file){toast('No file chosen','false');return;}
  const cmap={};document.querySelectorAll('.map-sel').forEach(s=>{const c=s.id.replace('m_','');if(s.value)cmap[c]=s.value;});
  document.getElementById('imp-result').innerHTML='<span style="color:#0891b2">Importing… ~2s per row for geocoding. Do not close this tab.</span>';
  const fd=new FormData();fd.append('token',tok()||'broker');fd.append('file',_file);fd.append('column_map',JSON.stringify(cmap));
  const r=await fetch('/upload',{method:'POST',body:fd});const d=await r.json();
  document.getElementById('imp-result').innerHTML='<span style="color:#059669">✓ '+d.message+'</span>';
}
</script>"""
    return _broker_page("Upload Properties CSV", content, active="UPL", scripts=scripts)


# ── Broker: my listings (view + edit price + availability) ───────────────────

@app.get("/broker/properties")
async def broker_list_properties(token: str, broker: str | None = None):
    _check_broker_token(token)
    try:
        return {"properties": list_properties(broker_id=broker)}
    except Exception as e:
        logger.error(f"broker_list_properties error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class PropertyUpdate(BaseModel):
    token: str
    price_inr: int | None = None
    status: str | None = None       # available | booked | sold | unavailable
    area_name: str | None = None
    bhk: int | None = None
    property_type: str | None = None
    furnishing: str | None = None
    area_sqft: int | None = None
    description: str | None = None
    address: str | None = None
    city: str | None = None
    amenities: str | None = None


@app.post("/broker/properties/{property_id}/update")
async def broker_update_property(property_id: str, req: PropertyUpdate):
    _check_broker_token(req.token)
    try:
        ok = update_property(
            property_id,
            price_inr=req.price_inr, status=req.status,
            area_name=req.area_name, bhk=req.bhk,
            property_type=req.property_type, furnishing=req.furnishing,
            area_sqft=req.area_sqft, description=req.description,
            amenities=([a.strip() for a in req.amenities.split(",") if a.strip()]
                       if req.amenities is not None else None),
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Property not found")
        if req.address is not None or req.city is not None or req.area_name is not None:
            enrichment = reenrich_property_location(
                property_id, req.address or req.area_name, req.city
            )
        else:
            enrichment = refresh_property_index(property_id)
        if not enrichment.get("ok"):
            raise HTTPException(status_code=400, detail=enrichment.get("error", "Index refresh failed"))
        return {"ok": True, "enrichment": enrichment}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"broker_update_property error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class PropertyDeleteReq(BaseModel):
    token: str


@app.delete("/broker/properties/{property_id}")
async def broker_delete_property(property_id: str, req: PropertyDeleteReq):
    """Hard delete: removes all Storage images then the DB row."""
    _check_broker_token(req.token)
    ok = delete_property(property_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Property not found")
    return {"ok": True}


@app.get("/broker/edit/{property_id}", response_class=HTMLResponse)
async def broker_edit_page(property_id: str):
    pid = property_id
    content = f"""
<div class="page-sm" style="padding:0">
<div class="card card-body">
<div class="row"><div><label class="field-label">Price (₹)</label><input id="e-price" class="input" type="number"></div>
<div><label class="field-label">BHK</label><input id="e-bhk" class="input" type="number" min="1" max="10"></div></div>
<div class="row"><div><label class="field-label">Area Name</label><input id="e-area" class="input"></div>
<div><label class="field-label">Area sqft</label><input id="e-sqft" class="input" type="number"></div></div>
<label class="field-label">Full Address</label><input id="e-addr" class="input" placeholder="Triggers geocode re-enrichment if changed">
<label class="field-label">City</label><input id="e-city" class="input" value="Lucknow">
<div class="row"><div><label class="field-label">Type</label>
<select id="e-type" class="input"><option>Flat</option><option>Independent House</option><option>Villa</option><option>Builder Floor</option><option>Plot</option><option>Shop</option></select></div>
<div><label class="field-label">Furnishing</label>
<select id="e-furn" class="input"><option>Furnished</option><option>Semi-Furnished</option><option>Unfurnished</option></select></div></div>
<label class="field-label">Amenities (comma-separated)</label><input id="e-amen" class="input">
<label class="field-label">Status</label>
<select id="e-status" class="input"><option value="available">Available</option><option value="booked">Booked</option><option value="sold">Sold</option><option value="unavailable">Unavailable</option></select>
<label class="field-label">Description</label><textarea id="e-desc" class="input" style="height:80px"></textarea>
<div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap">
<button class="btn btn-primary" onclick="save()">Save Changes</button>
<a href="/broker/images/{pid}" class="btn btn-ghost">📷 Manage Photos</a>
<button class="btn btn-danger" onclick="del()">Delete Property</button>
</div>
<div id="e-result" style="margin-top:10px;font-size:13px"></div>
</div></div>"""
    scripts = f"""<script>
const PID='{pid}';
async function loadProp(){{
  const t=tok();if(!t)return;
  const r=await fetch('/broker/properties?token='+encodeURIComponent(t));
  const d=await r.json();
  const p=(d.properties||[]).find(x=>x.id===PID);if(!p){{document.getElementById('e-result').textContent='Property not found';return;}}
  if(p.price_inr)document.getElementById('e-price').value=p.price_inr;
  if(p.bhk)document.getElementById('e-bhk').value=p.bhk;
  if(p.area_name)document.getElementById('e-area').value=p.area_name;
  if(p.property_type)document.getElementById('e-type').value=p.property_type;
  if(p.status)document.getElementById('e-status').value=p.status;
}}
async function save(){{
  const t=tok();
  const body={{token:t}};
  const price=document.getElementById('e-price').value;if(price)body.price_inr=+price;
  const bhk=document.getElementById('e-bhk').value;if(bhk)body.bhk=+bhk;
  const area=document.getElementById('e-area').value;if(area)body.area_name=area;
  const sqft=document.getElementById('e-sqft').value;if(sqft)body.area_sqft=+sqft;
  const addr=document.getElementById('e-addr').value;if(addr)body.address=addr;
  const city=document.getElementById('e-city').value;if(city)body.city=city;
  body.property_type=document.getElementById('e-type').value;
  body.furnishing=document.getElementById('e-furn').value;
  body.status=document.getElementById('e-status').value;
  const amen=document.getElementById('e-amen').value;if(amen)body.amenities=amen;
  const desc=document.getElementById('e-desc').value;if(desc)body.description=desc;
  const r=await fetch('/broker/properties/'+PID+'/update',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
  const d=await r.json();
  document.getElementById('e-result').innerHTML=d.ok?'<span style="color:#059669">✓ Saved!'+(d.enrichment_ran?' (location re-enriched)':'')+'</span>':'<span style="color:#dc2626">Error</span>';
  toast(d.ok?'Saved successfully':'Save failed',d.ok);
}}
async function del(){{
  if(!confirm('Permanently delete this property and all photos? Cannot be undone.'))return;
  const r=await fetch('/broker/properties/'+PID,{{method:'DELETE',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:tok()}})}});
  if(r.ok){{toast('Deleted');window.location='/broker/listings';}}
  else toast('Delete failed','false');
}}
document.getElementById('tok-input').addEventListener('change',loadProp);
loadProp();
</script>"""
    return _broker_page(f"Edit Property", content, active="LIST", scripts=scripts,
        hdr_extra=f'<a href="/broker/images/{pid}" class="btn btn-ghost" style="font-size:12px;padding:7px 14px">📷 Photos</a>')


@app.get("/broker/listings", response_class=HTMLResponse)
async def broker_listings_page():
    content = """
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
  <select id="f-status" class="input" style="width:140px" onchange="load()">
    <option value="available">Available</option><option value="booked">Booked</option>
    <option value="sold">Sold</option><option value="">All</option>
  </select>
  <input class="input" id="f-area" placeholder="Filter by area…" style="width:180px" oninput="load()">
  <a href="/broker/add" class="btn btn-primary" style="margin-left:auto">+ Add Property</a>
  <a href="/broker/upload" class="btn btn-ghost">Upload CSV</a>
</div>
<div id="count" style="font-size:13px;color:#64748b;margin-bottom:10px"></div>
<div id="props"></div>
"""
    scripts = """<script>
async function load(){
  const t=tok();if(!t)return;
  const st=document.getElementById('f-status').value;
  const ar=document.getElementById('f-area').value.trim();
  const r=await fetch('/broker/properties?token='+encodeURIComponent(t));
  const d=await r.json();
  let ps=(d.properties||[]).filter(p=>(!st||p.status===st)&&(!ar||((p.area_name||'').toLowerCase().includes(ar.toLowerCase()))));
  document.getElementById('count').textContent=ps.length+' properties';
  if(!ps.length){document.getElementById('props').innerHTML='<div class="empty"><div class="empty-icon">🏠</div><p>No properties found.</p></div>';return;}
  document.getElementById('props').innerHTML='<div class="card"><table class="table"><thead><tr><th>Property</th><th>Area</th><th>Price</th><th>Status</th><th>Images</th><th>Actions</th></tr></thead><tbody>'
    +ps.map(p=>`<tr>
      <td><b>${p.bhk||''}${p.bhk?' BHK ':''} ${p.property_type||''}</b><br><span style="font-size:11px;color:#94a3b8">${p.id.replace('rag_property_','')}</span></td>
      <td>${p.area_name||'—'}</td>
      <td style="font-weight:700;color:#2563eb">${fmt(p.price_inr)}</td>
      <td><span class="badge ${p.status==='available'?'badge-won':p.status==='sold'?'badge-lost':'badge-wait'}">${p.status}</span></td>
      <td style="text-align:center">${p.images||0} 📷</td>
      <td style="display:flex;gap:6px;flex-wrap:wrap">
        <a href="/broker/edit/${p.id}" class="btn btn-ghost" style="font-size:11px;padding:4px 10px">Edit</a>
        <a href="/broker/images/${p.id}" class="btn btn-ghost" style="font-size:11px;padding:4px 10px">Photos</a>
        ${p.status!=='sold'?`<button class="btn btn-danger" style="font-size:11px;padding:4px 10px" onclick="markSold('${p.id}')">Sold</button>`:''}
      </td>
    </tr>`).join('')
    +'</tbody></table></div>';
}
async function markSold(id){if(!confirm('Mark as sold?'))return;await fetch('/broker/properties/'+id+'/sold',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok()})});toast('Marked sold');load();}
document.getElementById('tok-input').addEventListener('change',load);
load();
</script>"""
    return _broker_page("My Listings", content, active="LIST",
        hdr_extra='<a href="/broker/add" class="btn btn-primary" style="font-size:12px;padding:7px 14px">+ Add Property</a>',
        scripts=scripts)


# ── Visit reminders (call daily from a free cron / n8n / Railway scheduler) ──

@app.post("/cron/send-reminders")
async def cron_send_reminders(token: str, hours_ahead: int = 24):
    _check_broker_token(token)
    from notifications.reminders import send_due_reminders
    return send_due_reminders(hours_ahead=hours_ahead)


# ── Broker: analytics ────────────────────────────────────────────────────────

@app.get("/broker/analytics")
async def broker_analytics(token: str):
    _check_broker_token(token)
    from datetime import datetime, timezone, timedelta
    from collections import Counter
    try:
        leads = get_all_leads(limit=1000)
        props = list_properties(limit=1000)
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        def _recent(l):
            try:
                return datetime.fromisoformat(str(l.get("created_at")).replace("Z", "+00:00")) >= week_ago
            except Exception:
                return False

        by_status = Counter((l.get("status") or "new").lower() for l in leads)
        converted = by_status.get("won", 0)
        top_areas = Counter(l.get("preferred_area") for l in leads if l.get("preferred_area")).most_common(5)
        prop_status = Counter((p.get("status") or "available").lower() for p in props)

        return {
            "leads_total": len(leads),
            "leads_this_week": sum(1 for l in leads if _recent(l)),
            "leads_by_status": dict(by_status),
            "conversion_rate": round(100 * converted / len(leads), 1) if leads else 0,
            "top_areas": [{"area": a, "count": c} for a, c in top_areas],
            "properties_total": len(props),
            "properties_available": prop_status.get("available", 0),
            "properties_sold": prop_status.get("sold", 0) + prop_status.get("booked", 0),
        }
    except Exception as e:
        logger.error(f"broker_analytics error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/broker/analytics/visual", response_class=HTMLResponse)
async def broker_analytics_visual():
    content = """
<div class="grid-3" id="kpis" style="margin-bottom:20px"></div>
<div class="grid-2" id="charts">
  <div class="card card-body"><h3 style="font-size:13px;font-weight:700;margin-bottom:12px">Lead Funnel</h3><canvas id="cFunnel" height="200"></canvas></div>
  <div class="card card-body"><h3 style="font-size:13px;font-weight:700;margin-bottom:12px">Top Areas</h3><canvas id="cAreas" height="200"></canvas></div>
  <div class="card card-body"><h3 style="font-size:13px;font-weight:700;margin-bottom:12px">BHK Demand</h3><canvas id="cBhk" height="200"></canvas></div>
  <div class="card card-body"><h3 style="font-size:13px;font-weight:700;margin-bottom:12px">New Leads per Week</h3><canvas id="cWeekly" height="200"></canvas></div>
  <div class="card card-body"><h3 style="font-size:13px;font-weight:700;margin-bottom:12px">Property Types</h3><canvas id="cTypes" height="200"></canvas></div>
</div>
"""
    scripts = """<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
const C=['#2563eb','#0891b2','#7c3aed','#d97706','#16a34a','#dc2626','#64748b','#ea580c'];
async function loadAnalytics(){
  const t=tok();if(!t){document.getElementById('kpis').innerHTML='<p style="color:#94a3b8;grid-column:1/-1">Enter broker token in sidebar.</p>';return;}
  const r=await fetch('/api/broker/analytics/charts?token='+encodeURIComponent(t));
  if(!r.ok){document.getElementById('kpis').innerHTML='<p style="color:#dc2626;grid-column:1/-1">Invalid token.</p>';return;}
  const d=await r.json();
  const s=d.summary||{};
  document.getElementById('kpis').innerHTML=[
    ['Total Leads',s.total_leads||0,'#2563eb'],['Meetings',s.meetings||0,'#0891b2'],
    ['Won',s.won||0,'#16a34a'],[`${s.conversion_pct||0}%`,'Conversion','#d97706'],
    ['Properties',s.properties||0,'#334155'],['Available',s.available_props||0,'#059669'],
  ].map(([v,l,c])=>`<div class="stat"><div class="stat-val" style="color:${c}">${v}</div><div class="stat-lbl">${l}</div></div>`).join('');
  const mk=(id,type,labels,data,lbl)=>new Chart(document.getElementById(id),{type,data:{labels,datasets:[{label:lbl||'',data,backgroundColor:type==='line'?C[0]+'33':C,borderColor:C[0],fill:type==='line',tension:.3}]},options:{plugins:{legend:{display:type==='doughnut',position:'right'}},scales:type!=='doughnut'?{y:{beginAtZero:true,ticks:{precision:0}}}:{}}});
  const f=d.funnel||[];mk('cFunnel','bar',f.map(x=>x.stage),f.map(x=>x.count));
  const a=d.top_areas||[];new Chart(document.getElementById('cAreas'),{type:'bar',data:{labels:a.map(x=>x.area),datasets:[{data:a.map(x=>x.count),backgroundColor:C}]},options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{beginAtZero:true,ticks:{precision:0}}}}});
  const b=d.bhk||[];mk('cBhk','doughnut',b.map(x=>x.label),b.map(x=>x.count));
  const w=d.weekly||[];mk('cWeekly','line',w.map(x=>x.week),w.map(x=>x.count),'Leads');
  const pt=d.prop_types||[];mk('cTypes','doughnut',pt.map(x=>x.type),pt.map(x=>x.count));
}
document.getElementById('tok-input').addEventListener('change',loadAnalytics);
loadAnalytics();
</script>"""
    return _broker_page("Analytics", content, active="ANAL", scripts=scripts)


@app.get("/api/broker/analytics/charts")
async def broker_analytics_charts(token: str):
    """Rich analytics data for the visual dashboard."""
    _check_broker_token(token)
    from database.supabase_client import get_client
    from collections import Counter
    import datetime

    cl = get_client()
    leads = cl.table("leads").select("status,created_at,preferred_area,preferred_bhk,budget_max").execute().data or []
    meetings = cl.table("meetings").select("status,scheduled_at,created_at").execute().data or []
    props = cl.table("properties").select("status,property_type,area_name").execute().data or []

    stage_order = ["new","contacted","visit","met","negotiating","won","waiting","lost"]
    stage_labels = {"new":"New","contacted":"Contacted","visit":"Scheduled","met":"Visited",
                    "negotiating":"Negotiating","won":"Won","waiting":"Waiting","lost":"Lost"}
    funnel = {s: 0 for s in stage_order}
    for l in leads:
        st = (l.get("status") or "new").lower()
        if st in funnel:
            funnel[st] += 1

    area_counts = Counter(
        l.get("preferred_area","").split(",")[0].strip()
        for l in leads if l.get("preferred_area")
    )
    top_areas = [{"area": k, "count": v} for k, v in area_counts.most_common(8) if k]

    bhk_counts = Counter(str(l.get("preferred_bhk","?")) for l in leads if l.get("preferred_bhk"))
    bhk_data = [{"label": k+"BHK", "count": v} for k, v in sorted(bhk_counts.items())]

    now = datetime.datetime.utcnow()
    weekly = Counter()
    for l in leads:
        try:
            dt = datetime.datetime.fromisoformat(l["created_at"].replace("Z",""))
            w = (now - dt).days // 7
            if 0 <= w < 8:
                weekly[w] += 1
        except Exception:
            pass
    weekly_data = [{"week": f"W-{i}", "count": weekly.get(i,0)} for i in range(7,-1,-1)]

    prop_status = Counter((p.get("status") or "available").lower() for p in props)
    prop_types = Counter((p.get("property_type") or "other").lower() for p in props)

    total = len(leads)
    won = sum(1 for l in leads if (l.get("status") or "").lower() == "won")

    return {
        "summary": {
            "total_leads": total,
            "meetings": sum(1 for m in meetings if (m.get("status") or "").lower() == "confirmed"),
            "properties": len(props), "won": won,
            "conversion_pct": round(won/total*100,1) if total else 0,
            "available_props": prop_status.get("available",0),
        },
        "funnel": [{"stage": stage_labels.get(s,s), "count": funnel[s]} for s in stage_order],
        "top_areas": top_areas,
        "bhk": bhk_data,
        "weekly": weekly_data,
        "prop_types": [{"type": k, "count": v} for k, v in prop_types.most_common(6)],
    }











def _image_manager_html(property_id: str) -> str:
    pid = property_id
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Images</title><style>"
        "*{box-sizing:border-box;margin:0;padding:0}"
        "body{font-family:system-ui,sans-serif;background:#f1f5f9;padding:16px;color:#0f172a}"
        "h1{font-size:18px;color:#1d4ed8;margin-bottom:4px}"
        ".sub{color:#64748b;font-size:12px;margin-bottom:14px}"
        ".bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}"
        ".bar input{padding:8px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px;width:160px}"
        "button{cursor:pointer;border:none;border-radius:8px;padding:8px 12px;font-size:13px;font-weight:600}"
        ".bp{background:#1d4ed8;color:#fff}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:16px}"
        ".ic{background:#fff;border:2px solid #e2e8f0;border-radius:10px;overflow:hidden;position:relative;cursor:grab}"
        ".ic img{width:100%;height:100px;object-fit:cover}"
        ".ic.hero{border-color:#1d4ed8}"
        ".hb{position:absolute;top:4px;left:4px;background:#1d4ed8;color:#fff;font-size:10px;font-weight:700;padding:2px 6px;border-radius:99px}"
        ".xb{position:absolute;top:4px;right:4px;background:#ef4444;color:#fff;border:none;border-radius:99px;width:22px;height:22px;cursor:pointer;font-size:13px}"
        ".drop{border:2px dashed #cbd5e1;border-radius:10px;padding:20px;text-align:center;color:#64748b;cursor:pointer;margin-bottom:14px}"
        ".st{font-size:13px;color:#0f766e;margin-top:8px}"
        "</style></head><body>"
        "<h1>Images &mdash; " + pid + "</h1>"
        "<div class='sub'>Drag to reorder &middot; First = hero photo &middot; Uploading real images removes Unsplash placeholders</div>"
        "<div class='bar'>"
        "<input type='password' id='tok' placeholder='Broker token'>"
        "<button class='bp' onclick='load()'>Load</button>"
        "<button class='bp' onclick='document.getElementById(\"fp\").click()'>+ Upload</button>"
        "<button class='bp' onclick='saveOrd()'>Save order</button>"
        "</div>"
        "<input id='fp' type='file' accept='image/*' multiple style='display:none' onchange='upFiles()'>"
        "<div class='drop' onclick='document.getElementById(\"fp\").click()'>Click or drag photos here (max 10 at once, 5MB each)</div>"
        "<div id='grid' class='grid'></div><div id='st' class='st'></div>"
        "<script>"
        "const PID='" + pid + "';let ORIG_ADDRESS='',ORIG_CITY='Lucknow',ORIG_AREA='';"
        "function tok(){return document.getElementById('tok').value.trim();}"
        "async function load(){"
        "  const r=await fetch('/properties/'+PID+'/images');const d=await r.json();"
        "  render(d.images||[]);localStorage.setItem('btok',tok());}"
        "function render(urls){"
        "  const g=document.getElementById('grid');g.innerHTML='';"
        "  urls.forEach((u,i)=>{"
        "    const c=document.createElement('div');c.className='ic'+(i===0?' hero':'');c.draggable=true;c.dataset.url=u;"
        "    c.innerHTML=(i===0?'<span class=\"hb\">HERO</span>':'')"
        "      +'<img src=\"'+u+'?w=200\" onerror=\"this.src=\\''+u+'\\'\">' "
        "      +'<button class=\"xb\" onclick=\"del(\\''+encodeURIComponent(u)+'\\')\">&times;</button>';"
        "    c.addEventListener('dragstart',e=>e.dataTransfer.setData('idx',i));"
        "    c.addEventListener('dragover',e=>e.preventDefault());"
        "    c.addEventListener('drop',e=>{e.preventDefault();const from=+e.dataTransfer.getData('idx');"
        "      const items=[...g.children];const mv=items[from];g.removeChild(mv);g.insertBefore(mv,items[i]);reIdx();});"
        "    g.appendChild(c);});}"
        "function reIdx(){const cs=[...document.querySelectorAll('.ic')];cs.forEach((c,i)=>{"
        "  c.classList.toggle('hero',i===0);const b=c.querySelector('.hb');"
        "  if(i===0){if(!b){const nb=document.createElement('span');nb.className='hb';nb.textContent='HERO';c.prepend(nb);}}"
        "  else{if(b)b.remove();}});}"
        "async function saveOrd(){"
        "  const urls=[...document.querySelectorAll('.ic')].map(c=>c.dataset.url);"
        "  await fetch('/properties/'+PID+'/images/reorder',{method:'POST',headers:{'Content-Type':'application/json'},"
        "    body:JSON.stringify({token:tok(),ordered_urls:urls})});"
        "  document.getElementById('st').textContent='Order saved!';setTimeout(()=>document.getElementById('st').textContent='',2000);}"
        "async function del(url){"
        "  if(!confirm('Delete this image?'))return;"
        "  await fetch('/properties/'+PID+'/images',{method:'DELETE',headers:{'Content-Type':'application/json'},"
        "    body:JSON.stringify({token:tok(),image_url:decodeURIComponent(url)})});load();}"
        "async function upFiles(){"
        "  const files=document.getElementById('fp').files;if(!files.length)return;"
        "  document.getElementById('st').textContent='Uploading '+files.length+' image(s)...';"
        "  const fd=new FormData();fd.append('token',tok());"
        "  for(const f of files)fd.append('files',f);"
        "  const r=await fetch('/properties/'+PID+'/images/multi',{method:'POST',body:fd});"
        "  const d=await r.json();"
        "  document.getElementById('st').textContent=d.uploaded+' image(s) uploaded!';load();}"
        "window.onload=()=>{const s=localStorage.getItem('btok');if(s)document.getElementById('tok').value=s;load();};"
        "</script></body></html>"
    )


def _broker_edit_html(property_id: str) -> str:
    pid = property_id
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Edit Property</title><style>"
        "*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:#f1f5f9;padding:18px;max-width:600px;margin:auto}"
        "h1{font-size:20px;color:#1d4ed8;margin-bottom:4px}.sub{color:#64748b;font-size:13px;margin-bottom:14px}"
        ".box{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin-bottom:14px}"
        "label{display:block;font-size:12px;font-weight:600;color:#475569;margin:8px 0 3px}"
        "input,select,textarea{width:100%;padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px}"
        "textarea{height:80px;resize:vertical}"
        ".row{display:flex;gap:10px}.row>div{flex:1}"
        "button{cursor:pointer;border:none;border-radius:8px;padding:9px 14px;font-size:14px;font-weight:700}"
        ".bp{background:#1d4ed8;color:#fff}.bd{background:#fee2e2;color:#991b1b;margin-left:8px}"
        ".st{font-size:13px;margin-top:8px}"
        "</style></head><body>"
        "<h1>Edit Property</h1>"
        "<div class='sub'><a href='/broker/listings'>← My listings</a> &nbsp;·&nbsp; "
        "<a href='/broker/images/" + pid + "'>Manage photos</a></div>"
        "<div class='box'>"
        "<label>Broker token</label><input id='tok' type='password' placeholder='token'>"
        "<div class='row'>"
        "<div><label>Price (₹)</label><input id='price' type='number' placeholder='e.g. 8500000'></div>"
        "<div><label>BHK</label><input id='bhk' type='number' min='1' max='10' placeholder='3'></div>"
        "</div>"
        "<div class='row'>"
        "<div><label>Area name</label><input id='area' placeholder='Gomti Nagar'></div>"
        "<div><label>Area sqft</label><input id='sqft' type='number' placeholder='1200'></div>"
        "</div>"
        "<div class='row'>"
        "<div><label>Full address (recalculates distances)</label><input id='address' placeholder='Gomti Nagar, Lucknow'></div>"
        "<div><label>City</label><input id='city' value='Lucknow'></div>"
        "</div>"
        "<div class='row'>"
        "<div><label>Type</label><select id='type'><option>Flat</option><option>Independent House</option>"
        "<option>Villa</option><option>Builder Floor</option><option>Plot</option><option>Shop</option></select></div>"
        "<div><label>Furnishing</label><select id='furn'><option>Furnished</option>"
        "<option>Semi-Furnished</option><option>Unfurnished</option></select></div>"
        "</div>"
        "<label>Status</label><select id='status'><option value='available'>Available</option>"
        "<option value='booked'>Booked</option><option value='sold'>Sold</option>"
        "<option value='unavailable'>Unavailable</option></select>"
        "<label>Description</label><textarea id='desc' placeholder='Update property details...'></textarea>"
        "<label>Amenities (comma-separated)</label><input id='amenities' placeholder='Lift, Parking, Gym'></input>"
        "<div style='margin-top:12px'>"
        "<button class='bp' onclick='save()'>Save changes</button>"
        "<button class='bd' onclick='del()'>Delete property</button>"
        "</div>"
        "<div id='st' class='st'></div>"
        "</div>"
        "<script>"
        "const PID='" + pid + "';"
        "function tok(){return document.getElementById('tok').value.trim();}"
        "async function load(){"
        "  const r=await fetch('/broker/properties?token='+encodeURIComponent(tok()));"
        "  const d=await r.json();const p=(d.properties||[]).find(x=>x.id===PID);"
        "  if(!p){document.getElementById('st').textContent='Property not found';return;}"
        "  if(p.price_inr)document.getElementById('price').value=p.price_inr;"
        "  if(p.bhk)document.getElementById('bhk').value=p.bhk;"
        "  if(p.area_name){document.getElementById('area').value=p.area_name;ORIG_AREA=p.area_name;}"
        "  if(p.address){document.getElementById('address').value=p.address;ORIG_ADDRESS=p.address;}"
        "  if(p.city){document.getElementById('city').value=p.city;ORIG_CITY=p.city;}"
        "  if(p.area_sqft)document.getElementById('sqft').value=p.area_sqft;"
        "  if(p.furnishing)document.getElementById('furn').value=p.furnishing;"
        "  if(p.description)document.getElementById('desc').value=p.description;"
        "  if(p.amenities)document.getElementById('amenities').value=p.amenities.join(', ');"
        "  if(p.property_type)document.getElementById('type').value=p.property_type;"
        "  if(p.status)document.getElementById('status').value=p.status;"
        "}"
        "async function save(){"
        "  const body={token:tok()};"
        "  const price=document.getElementById('price').value;if(price)body.price_inr=+price;"
        "  const bhk=document.getElementById('bhk').value;if(bhk)body.bhk=+bhk;"
        "  const area=document.getElementById('area').value;if(area&&area!==ORIG_AREA)body.area_name=area;"
        "  const address=document.getElementById('address').value;if(address&&address!==ORIG_ADDRESS)body.address=address;"
        "  const city=document.getElementById('city').value;if(city&&city!==ORIG_CITY)body.city=city;"
        "  const sqft=document.getElementById('sqft').value;if(sqft)body.area_sqft=+sqft;"
        "  body.property_type=document.getElementById('type').value;"
        "  body.furnishing=document.getElementById('furn').value;"
        "  body.status=document.getElementById('status').value;"
        "  const desc=document.getElementById('desc').value;if(desc)body.description=desc;"
        "  body.amenities=document.getElementById('amenities').value;"
        "  const r=await fetch('/broker/properties/'+PID+'/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});"
        "  const d=await r.json();"
        "  const geo=d.enrichment&&d.enrichment.connectivity;"
        "  document.getElementById('st').textContent=d.ok?(geo&&geo.status==='enriched'?'Saved! Coordinates and nearby distances recalculated.':'Saved and search index refreshed.'):'Error saving';}"
        "async function del(){"
        "  if(!confirm('Permanently delete this property and all its images? This cannot be undone.'))return;"
        "  const r=await fetch('/broker/properties/'+PID,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok()})});"
        "  if(r.ok)window.location='/broker/listings';"
        "  else document.getElementById('st').textContent='Delete failed';}"
        "window.onload=()=>{const s=localStorage.getItem('btok');if(s){document.getElementById('tok').value=s;load();}else{document.getElementById('tok').addEventListener('change',load);}};;"
        "</script></body></html>"
    )





_BROWSE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Browse Properties — Riya Lucknow</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:#f1f5f9;color:#0f172a}
header{background:#1d4ed8;color:#fff;padding:14px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:20px;flex:1}header a{color:#bfdbfe;font-size:13px;text-decoration:none}
.filters{display:flex;gap:8px;flex-wrap:wrap;padding:14px 20px;background:#fff;border-bottom:1px solid #e2e8f0}
.filters input,.filters select{padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px}
.filters button{padding:8px 14px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-weight:700;cursor:pointer;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;padding:16px 20px}
.card{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card img{width:100%;height:170px;object-fit:cover;background:#e2e8f0}
.cbody{padding:12px}
.ctitle{font-weight:700;font-size:15px}.cprice{color:#1d4ed8;font-weight:800;font-size:16px;margin:4px 0}
.cmeta{color:#64748b;font-size:12px;margin-bottom:8px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
.chip{background:#f1f5f9;border-radius:6px;padding:2px 7px;font-size:11px;color:#475569}
.actions{display:flex;gap:6px}.btn-chat{flex:1;padding:8px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-weight:700;font-size:13px;cursor:pointer;text-align:center;text-decoration:none;display:block}
.btn-chat:hover{background:#1558b0}.empty{text-align:center;padding:60px;color:#64748b}
#count{color:#64748b;font-size:13px;padding:6px 20px}
</style></head><body>
<header>
  <div><h1>Riya — Properties in Lucknow</h1><div style="font-size:12px;color:#bfdbfe">Find your dream home</div></div>
  <a href="/">💬 Chat with Riya</a>
</header>
<div class="filters">
  <input id="area" placeholder="Area (e.g. Gomti Nagar)" style="width:170px">
  <select id="bhk"><option value="">Any BHK</option><option>1</option><option>2</option><option>3</option><option>4</option></select>
  <select id="type"><option value="">Any type</option><option>Flat</option><option>Independent House</option><option>Villa</option><option>Builder Floor</option><option>Plot</option></select>
  <input id="min" type="number" placeholder="Min ₹ (lakh)" style="width:110px">
  <input id="max" type="number" placeholder="Max ₹ (lakh)" style="width:110px">
  <button onclick="load()">Search</button>
</div>
<div id="count"></div>
<div id="grid" class="grid"></div>
<script>
async function load(){
  const area=document.getElementById('area').value.trim();
  const bhk=document.getElementById('bhk').value;
  const type=document.getElementById('type').value;
  const minL=parseFloat(document.getElementById('min').value)||0;
  const maxL=parseFloat(document.getElementById('max').value)||0;
  let url='/api/properties/browse?limit=60';
  if(area)url+='&area='+encodeURIComponent(area);
  if(bhk)url+='&bhk='+bhk;
  if(type)url+='&prop_type='+encodeURIComponent(type);
  if(minL)url+='&min_price='+Math.round(minL*1e5);
  if(maxL)url+='&max_price='+Math.round(maxL*1e5);
  const r=await fetch(url);const d=await r.json();
  const ps=d.properties||[];
  document.getElementById('count').textContent=ps.length+' properties found';
  const g=document.getElementById('grid');
  if(!ps.length){g.innerHTML='<div class="empty">No properties found — try different filters or <a href=\"/\">chat with Riya</a>.</div>';return;}
  g.innerHTML='';
  ps.forEach(p=>{
    const c=document.createElement('div');c.className='card';
    const chips=(p.amenities||[]).map(a=>'<span class="chip">'+a+'</span>').join('');
    const metro=p.metro?'📍Metro '+p.metro+' km · ':'';
    c.innerHTML='<img src="'+(p.hero_img?p.hero_img+'?w=400':'')
      +'" onerror="this.style.background=\'#e2e8f0\'">'
      +'<div class="cbody">'
      +'<div class="ctitle">'+(p.bhk||'')+' BHK '+(p.prop_type||'')+' &mdash; '+(p.area||p.city||'Lucknow')+'</div>'
      +'<div class="cprice">'+(p.price_str||'POA')+'</div>'
      +'<div class="cmeta">'+(p.sqft?p.sqft+' sqft · ':'')+(p.furnishing||'')+' · '+metro+(p.city||'Lucknow')+'</div>'
      +'<div class="chips">'+chips+'</div>'
      +'<div class="actions"><a class="btn-chat" href="/?ask='+encodeURIComponent('Tell me more about '+p.id)+'" target="_blank">💬 Ask Riya about this</a></div>'
      +'</div>';
    g.appendChild(c);});
}
window.onload=load;
</script></body></html>"""








# ── Telegram webhook (production mode) ───────────────────────────────────────

_TELEGRAM_APP = None
_TELEGRAM_APP_LOCK = asyncio.Lock()


async def _get_telegram_app(token: str):
    global _TELEGRAM_APP
    if _TELEGRAM_APP is not None:
        return _TELEGRAM_APP
    async with _TELEGRAM_APP_LOCK:
        if _TELEGRAM_APP is not None:
            return _TELEGRAM_APP
        from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
        from interfaces.telegram_bot import (
            start, help_cmd, reset, profile_cmd, skip,
            handle_message, handle_voice, button_callback, shortlist_cmd,
        )
        app_tg = Application.builder().token(token).build()
        app_tg.add_handler(CommandHandler("start", start))
        app_tg.add_handler(CommandHandler("help", help_cmd))
        app_tg.add_handler(CommandHandler("reset", reset))
        app_tg.add_handler(CommandHandler("profile", profile_cmd))
        app_tg.add_handler(CommandHandler("skip", skip))
        app_tg.add_handler(CommandHandler("shortlist", shortlist_cmd))
        app_tg.add_handler(MessageHandler(filters.VOICE, handle_voice))
        app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app_tg.add_handler(CallbackQueryHandler(button_callback))
        await app_tg.initialize()
        _TELEGRAM_APP = app_tg
        return app_tg


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        from telegram import Update
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not set")
        webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        if webhook_secret and not hmac.compare_digest(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), webhook_secret
        ):
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")

        body = await request.json()
        app_tg = await _get_telegram_app(token)
        update = Update.de_json(body, app_tg.bot)
        await app_tg.process_update(update)

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Telegram webhook error: {e}")
        return {"ok": False}


# ── WhatsApp webhook ───────────────────────────────────────────────────────────

# Meta delivers webhooks AT LEAST once and may repeat the same message notification
# (network retries, multiple subscribed fields). Dedup by message-id so we never reply
# twice to one message. Bounded FIFO — single uvicorn worker, so in-memory is fine.
_wa_seen_msg_ids: dict[str, None] = {}

def _wa_already_seen(msg_id: str | None) -> bool:
    if not msg_id:
        return False
    if msg_id in _wa_seen_msg_ids:
        return True
    _wa_seen_msg_ids[msg_id] = None
    if len(_wa_seen_msg_ids) > 1000:
        # drop oldest ~200 to keep it bounded
        for old in list(_wa_seen_msg_ids)[:200]:
            _wa_seen_msg_ids.pop(old, None)
    return False


@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    params = dict(request.query_params)
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if not verify_token:
        raise HTTPException(status_code=503, detail="WHATSAPP_VERIFY_TOKEN is not configured")
    if params.get("hub.verify_token") == verify_token:
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_inbound(request: Request, background_tasks: BackgroundTasks):
    try:
        raw_body = await request.body()
        app_secret = os.environ.get("WHATSAPP_APP_SECRET")
        if app_secret:
            supplied = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                app_secret.encode("utf-8"), raw_body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(supplied, expected):
                raise HTTPException(status_code=403, detail="Invalid WhatsApp signature")
        body = json.loads(raw_body or b"{}")
        entry   = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value   = changes.get("value", {})
        messages = value.get("messages", [])

        # WhatsApp gives us the sender's profile name for free — use it to greet them.
        contacts = value.get("contacts", []) or []
        wa_name = None
        if contacts:
            wa_name = (contacts[0].get("profile") or {}).get("name")

        for msg in messages:
            msg_type = msg.get("type")
            sender   = msg.get("from")
            msg_id   = msg.get("id")
            session_id = f"wa_{sender}"

            # Skip duplicate deliveries of the same message (prevents double replies).
            if _wa_already_seen(msg_id):
                logger.info(f"WhatsApp duplicate message {msg_id} ignored")
                continue

            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
                if text:
                    background_tasks.add_task(_handle_whatsapp_message, sender, session_id, text, msg_id, wa_name)

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"WhatsApp inbound error: {e}")
        return {"ok": True}


def _wa_caption(card: dict) -> str:
    """One-line-ish caption for a property image on WhatsApp."""
    bits = []
    bhk = card.get("bhk")
    ptype = (card.get("property_type") or "").title()
    head = " ".join(x for x in [f"{bhk} BHK" if bhk else "", ptype] if x).strip()
    price = card.get("price_str") or ""
    area = card.get("area") or ""
    line1 = " • ".join(x for x in [head, price, area] if x)
    if line1:
        bits.append(f"*{line1}*")
    meta = []
    if card.get("sqft"):
        meta.append(f"{card['sqft']} sqft")
    if card.get("furnishing"):
        meta.append(str(card["furnishing"]).title())
    if card.get("floor"):
        meta.append(f"Floor {card['floor']}")
    if meta:
        bits.append(" • ".join(meta))
    if card.get("map_url"):
        bits.append(f"📍 {card['map_url']}")
    return "\n".join(bits)[:1020]


async def _handle_whatsapp_message(sender_phone: str, session_id: str, text: str,
                                   msg_id: str | None = None, wa_name: str | None = None):
    from notifications.whatsapp_notifier import _send, mark_read, send_image

    # Acknowledge instantly: blue ticks + "typing…" indicator while we work.
    # Best-effort — never let this block or break the actual reply.
    if msg_id:
        try:
            mark_read(msg_id, typing=True)
        except Exception as e:
            logger.warning(f"WhatsApp mark_read error: {e}")

    # If this number is the BROKER, route to the broker experience — never the buyer agent.
    try:
        from agent.broker_confirmation import (
            handle_broker_reply, is_configured_broker, handle_broker_command,
        )
        if is_configured_broker(sender_phone):
            if not handle_broker_reply(sender_phone, text):
                # Not a YES/NO/reschedule reply — give a broker menu/ack, not the buyer flow.
                _send(sender_phone, handle_broker_command(sender_phone, text))
            return
        # Non-broker: still let a pending confirmation match (defensive; returns False otherwise).
        if handle_broker_reply(sender_phone, text):
            return
    except Exception as e:
        logger.warning(f"broker routing failed: {e}")

    try:
        result = process_message(session_id=session_id, user_message=text,
                                 platform="whatsapp", display_name=wa_name,
                                 user_phone=sender_phone)
        _send(sender_phone, result["reply"])

        # Send up to 2 property photos so WhatsApp feels as visual as the web cards.
        for card in (result.get("properties") or [])[:2]:
            imgs = card.get("images") or []
            if imgs:
                try:
                    send_image(sender_phone, imgs[0], _wa_caption(card))
                except Exception as e:
                    logger.warning(f"WhatsApp image send failed: {e}")
    except Exception as e:
        logger.error(f"WhatsApp reply failed: {e}")
