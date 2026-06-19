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
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from agent.property_agent import process_message
from broker.upload_handler import process_csv, create_property_from_fields
from database.supabase_client import (
    update_lead_status, save_meeting, get_upcoming_meetings,
    upload_property_image, add_image_url_to_property, get_property_images,
    delete_property_image, reorder_property_images, replace_unsplash_images, delete_property,
    upload_property_document, add_document_to_property, get_property_documents,
    get_session, save_session, get_all_leads, mark_property_booked,
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


# ── Broker upload endpoint ────────────────────────────────────────────────────

@app.post("/upload/preview")
async def upload_preview(token: str = Form(...), file: UploadFile = File(...)):
    """
    Parse a broker CSV and return a preview of detected columns + sample rows
    WITHOUT importing. Broker reviews, corrects if needed, then calls /upload.
    """
    _check_broker_token(token)
    import csv, io, chardet
    raw = await file.read()
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
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted")

    content = await file.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="wb")
    tmp.write(content)
    tmp.close()

    parsed_map = {}
    if column_map:
        import json as _json
        try:
            parsed_map = _json.loads(column_map)
        except Exception:
            pass

    background_tasks.add_task(_run_upload, tmp.name, broker_id, parsed_map)

    return {
        "status": "processing",
        "message": f"File '{file.filename}' received. Each property will be geocoded and enriched with POI distances. This takes ~2s per row.",
        "broker_id": broker_id,
    }


def _run_upload(filepath: str, broker_id: str | None, column_map: dict | None = None) -> None:
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
        logger.info(f"Upload job complete: {result}")
    except Exception as e:
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
async def upcoming_meetings(hours_ahead: int = 24):
    meetings = get_upcoming_meetings(hours_ahead)
    return {"count": len(meetings), "meetings": meetings}


# ── Property image upload ─────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_SIZE_MB = 5


@app.post("/properties/{property_id}/images")
async def upload_image(property_id: str, file: UploadFile = File(...)):
    content_type = file.content_type or "image/jpeg"
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {content_type}. Use JPEG/PNG/WebP.")

    file_bytes = await file.read()
    if len(file_bytes) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Image too large — max {MAX_IMAGE_SIZE_MB} MB")

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
    filename = f"{file.filename or 'photo'}"

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
    uploaded = []
    for file in files[:10]:  # max 10 at once
        ct = file.content_type or "image/jpeg"
        if ct not in ALLOWED_IMAGE_TYPES:
            continue
        data = await file.read()
        if len(data) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
            continue
        url = upload_property_image(property_id, file.filename or "photo", data, ct)
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


@app.get("/broker/images/{property_id}", response_class=HTMLResponse)
async def broker_image_manager(property_id: str):
    """Simple drag-to-reorder, upload, delete image manager for one property."""
    return _image_manager_html(property_id)


# ── Simple web chat interface ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def web_chat():
    """Chat UI with property cards, image galleries, and shortlist."""
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

@app.get("/properties/browse", response_class=HTMLResponse)
async def properties_browse():
    return _BROWSE_HTML


@app.get("/api/properties/browse")
async def api_browse(area: str = "", bhk: int = None, min_price: int = None,
                     max_price: int = None, prop_type: str = "", limit: int = 30):
    """Public endpoint for the browse page — no auth, direct DB query."""
    from database.supabase_client import get_client
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
        q = q.ilike("area_name", f"%{area.replace(' ','')}%")
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
    return os.environ.get("BROKER_TOKEN", "broker")


def _check_broker_token(token: str | None):
    if token != _broker_token():
        raise HTTPException(status_code=401, detail="Invalid broker token")


@app.get("/broker/pipeline", response_class=HTMLResponse)
async def broker_pipeline_page():
    return _BROKER_PIPELINE_HTML


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


@app.post("/broker/leads/{lead_id}/status")
async def broker_update_lead(lead_id: str, req: LeadStatusUpdate):
    _check_broker_token(req.token)
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
        mark_property_booked(property_id)
        return {"ok": True}
    except Exception as e:
        logger.error(f"broker_mark_sold error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/broker", response_class=HTMLResponse)
async def broker_dashboard():
    return _BROKER_HTML


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
    return _BROKER_ADD_HTML


@app.get("/broker/upload", response_class=HTMLResponse)
async def broker_upload_page():
    return _BROKER_UPLOAD_HTML


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
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Property not found")
        return {"ok": True}
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
    return _broker_edit_html(property_id)


@app.get("/broker/listings", response_class=HTMLResponse)
async def broker_listings_page():
    return _BROKER_LISTINGS_HTML


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
        converted = by_status.get("converted", 0) + by_status.get("visit", 0)
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


_BROKER_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Broker Dashboard — Riya</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9;color:#0f172a;padding:18px}
h1{font-size:20px;color:#1d4ed8;margin-bottom:2px}.sub{color:#64748b;font-size:13px;margin-bottom:16px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.bar input{padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px}
.bar select{padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px}
button{cursor:pointer;border:none;border-radius:8px;padding:8px 12px;font-size:13px;font-weight:600}
.btn-load{background:#1d4ed8;color:#fff}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;margin-bottom:10px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.name{font-weight:700;font-size:16px}.phone{color:#0f766e;font-weight:600}
.meta{color:#475569;font-size:13px;margin-top:4px;line-height:1.6}
.pill{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:99px;text-transform:capitalize}
.s-new{background:#dbeafe;color:#1e40af}.s-contacted{background:#fef9c3;color:#854d0e}.s-visit{background:#e9d5ff;color:#6b21a8}.s-converted{background:#dcfce7;color:#166534}.s-lost{background:#fee2e2;color:#991b1b}
.actions{margin-top:10px;display:flex;gap:6px;flex-wrap:wrap}
.actions button{background:#f1f5f9;color:#334155;border:1px solid #e2e8f0}
.actions button:hover{background:#e2e8f0}
.empty{color:#64748b;text-align:center;padding:40px}
a.call{color:#0f766e;text-decoration:none;font-weight:600}
</style></head><body>
<h1>Broker Dashboard</h1><div class="sub">Riya — Real Estate AI · <a href="/broker/pipeline">Pipeline</a> · <a href="/broker/add">+ Add property</a> · <a href="/broker/upload">Upload CSV</a> · <a href="/broker/listings">My listings</a></div>
<div class="bar">
  <input id="tok" placeholder="Broker token" type="password">
  <select id="flt"><option value="all">All</option><option value="new">New</option><option value="contacted">Contacted</option><option value="visit">Visit</option><option value="converted">Converted</option><option value="lost">Lost</option></select>
  <button class="btn-load" onclick="load()">Load leads</button>
  <span id="count" style="color:#64748b;font-size:13px"></span>
</div>
<div id="stats" style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px"></div>
<div id="list"></div>
<script>
const STATUSES=['new','contacted','visit','converted','lost'];
function tok(){return document.getElementById('tok').value.trim();}
async function load(){
  const t=tok(); if(!t){alert('Enter the broker token');return;}
  const f=document.getElementById('flt').value;
  localStorage.setItem('btok',t);
  const r=await fetch(`/broker/leads?token=${encodeURIComponent(t)}&status=${f}`);
  if(!r.ok){document.getElementById('list').innerHTML='<div class="empty">Unauthorized or error.</div>';return;}
  const d=await r.json();const leads=d.leads||[];
  document.getElementById('count').textContent=leads.length+' lead'+(leads.length==1?'':'s');
  loadStats();
  const el=document.getElementById('list');
  if(!leads.length){el.innerHTML='<div class="empty">No leads yet.</div>';return;}
  el.innerHTML='';
  leads.forEach(L=>{
    const bud=(L.budget_max?('₹'+(L.budget_max/100000).toFixed(0)+'L'):'—');
    const when=L.created_at?new Date(L.created_at).toLocaleString():'';
    const st=(L.status||'new').toLowerCase();
    const card=document.createElement('div');card.className='card';
    card.innerHTML=`<div class="top">
        <div><div class="name">${L.name||'(no name)'} · <a class="call" href="tel:${L.phone||''}">${L.phone||'—'}</a></div>
          <div class="meta">${L.preferred_bhk?L.preferred_bhk+' BHK · ':''}${L.preferred_area||L.preferred_city||''} · budget ${bud}<br>
          ${L.interested_property_id?('🏠 '+L.interested_property_id):'<i>no property attached</i>'} · <span style="color:#94a3b8">${when}</span></div>
        </div><span class="pill s-${st}">${st}</span></div>
      <div class="actions">${STATUSES.map(s=>`<button onclick="setStatus('${L.id}','${s}')">${s}</button>`).join('')}</div>`;
    el.appendChild(card);
  });
}
async function setStatus(id,status){
  const r=await fetch(`/broker/leads/${id}/status`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok(),status})});
  if(r.ok){load();}else{alert('Update failed');}
}
async function loadStats(){
  const r=await fetch('/broker/analytics?token='+encodeURIComponent(tok()));
  if(!r.ok)return;const a=await r.json();
  const box=(label,val,col)=>`<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 14px;min-width:110px">
    <div style="font-size:22px;font-weight:800;color:${col||'#0f172a'}">${val}</div><div style="font-size:11px;color:#64748b">${label}</div></div>`;
  const areas=(a.top_areas||[]).map(x=>x.area+' ('+x.count+')').join(', ')||'—';
  document.getElementById('stats').innerHTML=
    box('Leads total',a.leads_total,'#1d4ed8')+box('This week',a.leads_this_week,'#0f766e')+
    box('Conversion',a.conversion_rate+'%','#166534')+box('Listings',a.properties_total)+
    box('Available',a.properties_available,'#166534')+box('Sold',a.properties_sold,'#991b1b')+
    `<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 14px;flex:1;min-width:180px">
      <div style="font-size:12px;color:#64748b">Top areas by interest</div><div style="font-weight:600;font-size:13px;margin-top:4px">${areas}</div></div>`;
}
window.onload=()=>{const s=localStorage.getItem('btok');if(s){document.getElementById('tok').value=s;load();}};
</script></body></html>"""


_BROKER_ADD_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Add Property — Riya Broker</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#f1f5f9;color:#0f172a;padding:18px;max-width:680px;margin:auto}
h1{font-size:20px;color:#1d4ed8}.sub{color:#64748b;font-size:13px;margin-bottom:16px}
.box{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin-bottom:14px}
label{display:block;font-size:12px;font-weight:600;color:#475569;margin:8px 0 3px}
input,select,textarea{width:100%;padding:9px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px}
.row{display:flex;gap:10px}.row>div{flex:1}
button{cursor:pointer;border:none;border-radius:8px;padding:10px 14px;font-size:14px;font-weight:700;background:#1d4ed8;color:#fff;margin-top:12px}
button.sec{background:#0f766e}
.muted{color:#64748b;font-size:12px}.ok{color:#166534;font-weight:600}.err{color:#991b1b;font-weight:600}
#step2,#step3{display:none}.drop{border:2px dashed #cbd5e1;border-radius:10px;padding:18px;text-align:center;color:#64748b;cursor:pointer}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.thumbs img{width:70px;height:70px;object-fit:cover;border-radius:8px}
.doc{font-size:13px;color:#0f766e;margin-top:6px}
a{color:#1d4ed8}
</style></head><body>
<h1>Add a Property</h1><div class="sub">Riya Broker · <a href="/broker">← leads dashboard</a></div>

<div class="box" id="step1">
  <label>Broker token</label><input id="tok" type="password" placeholder="token">
  <div class="row"><div><label>Type *</label>
    <select id="property_type"><option>Flat</option><option>Independent House</option><option>Villa</option><option>Builder Floor</option><option>Plot</option><option>Shop</option></select></div>
    <div><label>BHK</label><input id="bhk" type="number" min="0" placeholder="2"></div>
    <div><label>Price (₹) *</label><input id="price_inr" type="number" placeholder="5000000"></div></div>
  <div class="row"><div><label>Area (sqft)</label><input id="area_sqft" type="number" placeholder="1100"></div>
    <div><label>Furnishing</label><select id="furnishing"><option value="">—</option><option>Furnished</option><option>Semi-Furnished</option><option>Unfurnished</option></select></div></div>
  <label>Address / locality *</label><input id="address" placeholder="Gomti Nagar, near Phoenix Palassio">
  <label>Amenities (comma-separated)</label><input id="amenities" placeholder="Lift, Power Backup, Parking, Gym">
  <div class="row"><div><label>Your name</label><input id="broker_name" placeholder="Broker name"></div>
    <div><label>Your phone</label><input id="broker_phone" placeholder="98XXXXXXXX"></div></div>
  <label>Description</label><textarea id="description" rows="2" placeholder="Optional notes"></textarea>
  <button onclick="addProp()">Add property →</button>
  <div id="m1" class="muted" style="margin-top:8px"></div>
</div>

<div class="box" id="step2">
  <h1 style="font-size:16px">Photos</h1>
  <div class="muted">Property added. Now add photos (optional).</div>
  <div class="drop" onclick="document.getElementById('imgs').click()">Click to choose photos (JPG/PNG)</div>
  <input id="imgs" type="file" accept="image/*" multiple style="display:none" onchange="upImgs()">
  <div class="thumbs" id="thumbs"></div>
</div>

<div class="box" id="step3">
  <h1 style="font-size:16px">Documents</h1>
  <div class="muted">Floor plan, brochure, ownership papers, RERA cert (PDF/JPG).</div>
  <label>Label</label><input id="doclabel" placeholder="Floor plan">
  <div class="drop" onclick="document.getElementById('docs').click()">Click to choose a document</div>
  <input id="docs" type="file" accept="application/pdf,image/*" style="display:none" onchange="upDoc()">
  <div id="doclist"></div>
  <button class="sec" onclick="location.reload()">Done — add another</button>
</div>

<script>
let PID=null;
function v(id){return document.getElementById(id).value.trim();}
function tok(){return v('tok');}
async function addProp(){
  const m=document.getElementById('m1');m.textContent='Adding… (geocoding + indexing, ~3s)';m.className='muted';
  const body={token:tok(),property_type:v('property_type'),bhk:parseInt(v('bhk'))||null,price_inr:parseInt(v('price_inr'))||0,
    area_sqft:parseFloat(v('area_sqft'))||null,furnishing:v('furnishing')||null,address:v('address'),
    amenities:v('amenities')||null,broker_name:v('broker_name')||null,broker_phone:v('broker_phone')||null,description:v('description')||null};
  if(!body.address||!body.price_inr){m.textContent='Address and price are required.';m.className='err';return;}
  const r=await fetch('/broker/property',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok){m.textContent='Error: '+(d.detail||'failed');m.className='err';return;}
  PID=d.property_id;localStorage.setItem('btok',tok());
  m.innerHTML='<span class="ok">✓ Added in '+(d.area||'Lucknow')+'</span> (id '+PID+')';
  document.getElementById('step2').style.display='block';document.getElementById('step3').style.display='block';
  window.scrollTo(0,document.body.scrollHeight);
}
async function upImgs(){
  const files=document.getElementById('imgs').files;const t=document.getElementById('thumbs');
  for(const f of files){
    const fd=new FormData();fd.append('file',f);
    const r=await fetch('/properties/'+PID+'/images',{method:'POST',body:fd});
    if(r.ok){const d=await r.json();const im=document.createElement('img');im.src=d.image_url;t.appendChild(im);}
  }
}
async function upDoc(){
  const f=document.getElementById('docs').files[0];if(!f)return;
  const fd=new FormData();fd.append('file',f);fd.append('token',tok());fd.append('label',v('doclabel')||f.name);
  const r=await fetch('/broker/properties/'+PID+'/documents',{method:'POST',body:fd});
  const dl=document.getElementById('doclist');
  if(r.ok){const d=await r.json();dl.innerHTML+='<div class="doc">📄 <a href="'+d.document_url+'" target="_blank">'+d.label+'</a></div>';}
  else{const e=await r.json();dl.innerHTML+='<div class="err">'+(e.detail||'upload failed')+'</div>';}
}
window.onload=()=>{const s=localStorage.getItem('btok');if(s)document.getElementById('tok').value=s;};
</script></body></html>"""


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
        "const PID='" + pid + "';"
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
        "<div><label>Type</label><select id='type'><option>Flat</option><option>Independent House</option>"
        "<option>Villa</option><option>Builder Floor</option><option>Plot</option><option>Shop</option></select></div>"
        "<div><label>Furnishing</label><select id='furn'><option>Furnished</option>"
        "<option>Semi-Furnished</option><option>Unfurnished</option></select></div>"
        "</div>"
        "<label>Status</label><select id='status'><option value='available'>Available</option>"
        "<option value='booked'>Booked</option><option value='sold'>Sold</option>"
        "<option value='unavailable'>Unavailable</option></select>"
        "<label>Description</label><textarea id='desc' placeholder='Update property details...'></textarea>"
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
        "  if(p.area_name)document.getElementById('area').value=p.area_name;"
        "  if(p.property_type)document.getElementById('type').value=p.property_type;"
        "  if(p.status)document.getElementById('status').value=p.status;"
        "}"
        "async function save(){"
        "  const body={token:tok()};"
        "  const price=document.getElementById('price').value;if(price)body.price_inr=+price;"
        "  const bhk=document.getElementById('bhk').value;if(bhk)body.bhk=+bhk;"
        "  const area=document.getElementById('area').value;if(area)body.area_name=area;"
        "  const sqft=document.getElementById('sqft').value;if(sqft)body.area_sqft=+sqft;"
        "  body.property_type=document.getElementById('type').value;"
        "  body.furnishing=document.getElementById('furn').value;"
        "  body.status=document.getElementById('status').value;"
        "  const desc=document.getElementById('desc').value;if(desc)body.description=desc;"
        "  const r=await fetch('/broker/properties/'+PID+'/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});"
        "  const d=await r.json();"
        "  document.getElementById('st').textContent=d.ok?'Saved!':'Error saving';}"
        "async function del(){"
        "  if(!confirm('Permanently delete this property and all its images? This cannot be undone.'))return;"
        "  const r=await fetch('/broker/properties/'+PID,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok()})});"
        "  if(r.ok)window.location='/broker/listings';"
        "  else document.getElementById('st').textContent='Delete failed';}"
        "window.onload=()=>{const s=localStorage.getItem('btok');if(s){document.getElementById('tok').value=s;load();}else{document.getElementById('tok').addEventListener('change',load);}};;"
        "</script></body></html>"
    )


_BROKER_PIPELINE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lead Pipeline</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:#f1f5f9;color:#0f172a;min-height:100vh}
header{background:#1d4ed8;color:#fff;padding:12px 18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;flex:1}header a{color:#bfdbfe;font-size:12px;text-decoration:none}
.bar{display:flex;gap:8px;padding:10px 16px;background:#fff;border-bottom:1px solid #e2e8f0;align-items:center}
.bar input{padding:7px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px;width:160px}
.bar button{padding:7px 13px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer}
.board{display:flex;gap:10px;padding:14px 16px;overflow-x:auto;min-height:80vh}
.col{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;min-width:220px;flex:0 0 220px;display:flex;flex-direction:column}
.col-hdr{padding:10px 12px;font-weight:700;font-size:13px;border-bottom:1px solid #e2e8f0;display:flex;justify-content:space-between;align-items:center}
.col-hdr .cnt{background:#e2e8f0;border-radius:99px;padding:1px 8px;font-size:11px}
.cards{padding:8px;flex:1;min-height:60px}
.cards.drag-over{background:#eff6ff;border-radius:8px}
.lcard{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;margin-bottom:8px;cursor:grab;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.lcard:active{cursor:grabbing}.lcard.dragging{opacity:.4}
.lname{font-weight:700;font-size:14px}.lphone{color:#0f766e;font-size:12px;font-weight:600}
.lmeta{color:#64748b;font-size:11px;margin:3px 0 6px}
.lbtns{display:flex;gap:5px;flex-wrap:wrap}
.lbtns a,.lbtns button{font-size:11px;padding:3px 7px;border-radius:6px;border:1px solid #e2e8f0;background:#f8fafc;cursor:pointer;color:#334155;text-decoration:none}
.lbtns .wa{background:#dcfce7;border-color:#86efac;color:#166534}
.sold-warn{background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:8px 10px;font-size:12px;margin-top:6px;display:none}
</style></head><body>
<header>
  <h1>Lead Pipeline</h1>
  <a href="/broker">dashboard</a>
  <a href="/broker/leads?token=broker">list view</a>
</header>
<div class="bar">
  <input id="tok" type="password" placeholder="Broker token">
  <button onclick="load()">Load</button>
  <span id="st" style="font-size:12px;color:#64748b;margin-left:8px"></span>
</div>
<div class="board" id="board"></div>
<script>
const STAGES=[
  {id:'new',label:'New',color:'#1d4ed8'},
  {id:'contacted',label:'Contacted',color:'#0891b2'},
  {id:'visit',label:'Visit Scheduled',color:'#7c3aed'},
  {id:'met',label:'Site Visit Done',color:'#d97706'},
  {id:'negotiating',label:'Negotiating',color:'#ea580c'},
  {id:'won',label:'Bought / Won',color:'#16a34a'},
  {id:'waiting',label:'Waiting / Hold',color:'#6b7280'},
  {id:'lost',label:'Not Interested',color:'#dc2626'},
];
function tok(){return document.getElementById('tok').value.trim();}
let _leads=[];

async function load(){
  localStorage.setItem('btok',tok());
  const r=await fetch('/broker/leads?token='+encodeURIComponent(tok())+'&status=all');
  const d=await r.json();
  _leads=d.leads||[];
  document.getElementById('st').textContent=_leads.length+' leads';
  render();
}

function render(){
  const board=document.getElementById('board');
  board.innerHTML='';
  STAGES.forEach(s=>{
    const col=document.createElement('div');col.className='col';col.dataset.stage=s.id;
    const items=_leads.filter(l=>l.status===s.id);
    col.innerHTML='<div class="col-hdr" style="border-top:3px solid '+s.color+'"><span>'+s.label+'</span><span class="cnt">'+items.length+'</span></div><div class="cards" id="cards-'+s.id+'"></div>';
    board.appendChild(col);
    const wrap=document.getElementById('cards-'+s.id);
    items.forEach(l=>wrap.appendChild(makeCard(l)));
    // drag target
    wrap.addEventListener('dragover',e=>{e.preventDefault();wrap.classList.add('drag-over');});
    wrap.addEventListener('dragleave',()=>wrap.classList.remove('drag-over'));
    wrap.addEventListener('drop',e=>{
      e.preventDefault();wrap.classList.remove('drag-over');
      const id=e.dataTransfer.getData('lid');
      const newStatus=s.id;
      moveCard(id,newStatus);
    });
  });
}

function makeCard(l){
  const c=document.createElement('div');c.className='lcard';c.draggable=true;c.dataset.id=l.id;
  const phone=l.phone||'';
  const wa='https://wa.me/91'+phone.replace(/[^0-9]/g,'');
  const budget=l.budget_max?'up to Rs.'+(l.budget_max/1e7).toFixed(1)+'Cr':'';
  const prop=l.interested_property_id?l.interested_property_id.replace('rag_property_',''):'';
  c.innerHTML='<div class="lname">'+l.name+'</div>'
    +'<div class="lphone">'+phone+'</div>'
    +'<div class="lmeta">'+(l.preferred_bhk?l.preferred_bhk+'BHK · ':'')+budget+(prop?' · Prop: '+prop:'')+'</div>'
    +'<div class="lbtns">'
    +'<a href="'+wa+'" class="wa" target="_blank">WhatsApp</a>'
    +(phone?'<a href="tel:'+phone+'">Call</a>':'')
    +'<button onclick="note(\''+l.id+'\')">Note</button>'
    +'</div>';
  c.addEventListener('dragstart',e=>{e.dataTransfer.setData('lid',l.id);c.classList.add('dragging');});
  c.addEventListener('dragend',()=>c.classList.remove('dragging'));
  return c;
}

async function moveCard(leadId,newStatus){
  const r=await fetch('/broker/leads/'+leadId+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok(),status:newStatus})});
  if(r.ok){
    const lead=_leads.find(l=>l.id===leadId);
    if(lead)lead.status=newStatus;
    render();
    if(newStatus==='won'){
      const prop=lead&&lead.interested_property_id;
      if(prop&&confirm('Deal closed! Mark property '+prop+' as sold (removes from search)?')){
        await fetch('/broker/properties/'+prop+'/sold',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok()})});
      }
    }
  }
}

async function note(id){
  const txt=prompt('Add note:');if(!txt)return;
  await fetch('/broker/leads/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok(),status:_leads.find(l=>l.id===id)?.status,notes:txt})});
  load();
}

window.onload=()=>{const s=localStorage.getItem('btok');if(s)document.getElementById('tok').value=s;load();};
</script></body></html>"""


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


_BROKER_UPLOAD_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload Properties CSV</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:#f1f5f9;padding:18px;max-width:720px;margin:auto;color:#0f172a}
h1{font-size:20px;color:#1d4ed8}.sub{color:#64748b;font-size:13px;margin-bottom:14px}
.box{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin-bottom:14px}
label{display:block;font-size:12px;font-weight:600;color:#475569;margin:8px 0 3px}
input,select{width:100%;padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px}
button{cursor:pointer;border:none;border-radius:8px;padding:9px 14px;font-size:14px;font-weight:700;background:#1d4ed8;color:#fff}
.drop{border:2px dashed #cbd5e1;border-radius:10px;padding:20px;text-align:center;color:#64748b;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:5px 8px;text-align:left;border-bottom:1px solid #f1f5f9}
th{background:#f8fafc;font-weight:700}select.map{font-size:12px;padding:3px}
.ok{color:#166534;font-weight:600}.err{color:#991b1b;font-weight:600}
.note{background:#fef9c3;border:1px solid #fde68a;border-radius:8px;padding:10px;font-size:12px;margin-bottom:10px}
</style></head><body>
<h1>Upload Properties CSV</h1>
<div class="sub"><a href="/broker">← dashboard</a> · <a href="/broker/add">+ add single</a></div>
<div class="note">💡 Any column names work — Riya auto-detects them. Review the mapping below before importing.
Each property will be geocoded (lat/lng) and enriched with nearby metro/hospital/school distances automatically.</div>

<div class="box">
  <label>Broker token</label><input id="tok" type="password" placeholder="token">
  <label style="margin-top:10px">CSV file</label>
  <div class="drop" onclick="document.getElementById('csv').click()">Click to choose a CSV file</div>
  <input id="csv" type="file" accept=".csv" style="display:none" onchange="preview()">
</div>

<div id="preview-box" class="box" style="display:none">
  <b>Column mapping</b> <span id="map-note" style="font-size:12px;color:#64748b;margin-left:8px"></span>
  <table id="map-table" style="margin-top:8px"></table>
  <div id="sample-wrap" style="margin-top:12px"><b style="font-size:12px">Sample rows:</b>
    <div id="sample" style="overflow-x:auto;margin-top:6px"></div></div>
  <button style="margin-top:12px" onclick="doImport()">Import all rows</button>
  <div id="result" style="margin-top:8px"></div>
</div>

<script>
let _headers=[], _mapping={}, _file=null;
function tok(){return document.getElementById('tok').value.trim();}
async function preview(){
  _file=document.getElementById('csv').files[0];if(!_file)return;
  const fd=new FormData();fd.append('token',tok()||'broker');fd.append('file',_file);
  const r=await fetch('/upload/preview',{method:'POST',body:fd});
  if(!r.ok){alert('Preview failed: '+await r.text());return;}
  const d=await r.json();
  _headers=d.headers||[];_mapping=d.column_mapping||{};
  document.getElementById('preview-box').style.display='block';
  document.getElementById('map-note').textContent=d.total_rows+' rows · '+d.headers.length+' columns';
  // Column mapping table
  const canonical=['property_type','bhk','price_inr','area_sqft','address','city','furnishing','amenities','broker_name','broker_phone','description','external_ref'];
  const labels={'property_type':'Type *','bhk':'BHK','price_inr':'Price ₹ *','area_sqft':'Area sqft','address':'Address *','city':'City','furnishing':'Furnishing','amenities':'Amenities','broker_name':'Broker name','broker_phone':'Broker phone','description':'Description','external_ref':'Ref (idempotent key)'};
  let html='<tr><th>Field</th><th>Mapped to CSV column</th></tr>';
  canonical.forEach(c=>{
    const mapped=_mapping[c]||'';
    const opts=_headers.map(h=>'<option'+(h===mapped?' selected':'')+'>'+h+'</option>').join('');
    html+='<tr><td>'+labels[c]+'</td><td><select class="map" id="m_'+c+'"><option value="">— skip —</option>'+opts+'</select></td></tr>';
  });
  document.getElementById('map-table').innerHTML=html;
  // Sample table
  if(d.sample_rows&&d.sample_rows.length){
    let sh='<table><tr>'+_headers.map(h=>'<th>'+h+'</th>').join('')+'</tr>';
    d.sample_rows.forEach(row=>{sh+='<tr>'+_headers.map(h=>'<td>'+(row[h]||'')+'</td>').join('')+'</tr>';});
    document.getElementById('sample').innerHTML=sh+'</table>';
  }
}
async function doImport(){
  if(!_file){alert('No file chosen');return;}
  // build column_map from selects
  const cmap={};
  document.querySelectorAll('.map').forEach(s=>{
    const canon=s.id.replace('m_','');if(s.value)cmap[canon]=s.value;});
  document.getElementById('result').innerHTML='<span style="color:#0f766e">Importing... this may take 2-3s per property due to geocoding. Do not close this tab.</span>';
  const fd=new FormData();
  fd.append('token',tok()||'broker');
  fd.append('file',_file);
  fd.append('column_map',JSON.stringify(cmap));
  const r=await fetch('/upload',{method:'POST',body:fd});
  const d=await r.json();
  document.getElementById('result').innerHTML=
    '<span class="ok">'+d.message+'</span>';
}
window.onload=()=>{const s=localStorage.getItem('btok');if(s)document.getElementById('tok').value=s;};
</script></body></html>"""


_BROKER_LISTINGS_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Listings — Riya Broker</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#f1f5f9;color:#0f172a;padding:18px}
h1{font-size:20px;color:#1d4ed8}.sub{color:#64748b;font-size:13px;margin-bottom:14px}
.bar{display:flex;gap:8px;align-items:center;margin-bottom:14px}.bar input{padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px}
button{cursor:pointer;border:none;border-radius:7px;padding:7px 11px;font-size:13px;font-weight:600}.load{background:#1d4ed8;color:#fff}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;margin-bottom:9px}
.top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.t{font-weight:700}.meta{color:#475569;font-size:13px;margin-top:3px}
.pill{font-size:11px;font-weight:700;padding:3px 9px;border-radius:99px;text-transform:capitalize}
.s-available{background:#dcfce7;color:#166534}.s-booked{background:#fef9c3;color:#854d0e}.s-sold{background:#fee2e2;color:#991b1b}.s-unavailable{background:#e2e8f0;color:#475569}
.row{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;align-items:center}
.row input{width:130px;padding:6px 8px;border:1px solid #cbd5e1;border-radius:7px;font-size:13px}
.row button{background:#f1f5f9;color:#334155;border:1px solid #e2e8f0}
.empty{color:#64748b;text-align:center;padding:40px}a{color:#1d4ed8}
</style></head><body>
<h1>My Listings</h1><div class="sub">Riya Broker · <a href="/broker">leads</a> · <a href="/broker/add">+ add property</a></div>
<div class="bar"><input id="tok" type="password" placeholder="Broker token"><button class="load" onclick="load()">Load</button><span id="c" style="color:#64748b;font-size:13px"></span></div>
<div id="list"></div>
<script>
function tok(){return document.getElementById('tok').value.trim();}
function money(v){if(!v)return '—';return v>=1e7?('Rs.'+(v/1e7).toFixed(2)+' Cr'):('Rs.'+(v/1e5).toFixed(0)+' L');}
async function load(){
  const t=tok();if(!t){alert('Enter token');return;}localStorage.setItem('btok',t);
  const r=await fetch('/broker/properties?token='+encodeURIComponent(t));
  if(!r.ok){document.getElementById('list').innerHTML='<div class="empty">Unauthorized.</div>';return;}
  const d=await r.json();const ps=d.properties||[];
  document.getElementById('c').textContent=ps.length+' listings';
  const el=document.getElementById('list');if(!ps.length){el.innerHTML='<div class="empty">No listings.</div>';return;}
  el.innerHTML='';
  ps.forEach(p=>{
    const st=(p.status||'available').toLowerCase();
    const c=document.createElement('div');c.className='card';
    c.innerHTML=`<div class="top"><div>
       <div class="t">${p.bhk?p.bhk+' BHK ':''}${p.property_type||''} · ${p.area_name||''}</div>
       <div class="meta">${money(p.price_inr)} · 📷 ${p.images} · 📄 ${p.documents} · <a href="/broker/images/${p.id}" style="color:#1d4ed8;font-size:11px">Photos</a> · <a href="/broker/edit/${p.id}" style="color:#7c3aed;font-size:11px">Edit / Delete</a></div></div>
       <span class="pill s-${st}">${st}</span></div>
      <div class="row">
        <input type="number" id="pr_${p.id}" placeholder="new price ₹" value="${p.price_inr||''}">
        <button onclick="setPrice('${p.id}')">Save price</button>
        <button onclick="setStatus('${p.id}','available')">Available</button>
        <button onclick="setStatus('${p.id}','sold')">Sold</button>
        <button onclick="setStatus('${p.id}','unavailable')">Hide</button>
      </div>`;
    el.appendChild(c);
  });
}
async function upd(id,body){
  body.token=tok();
  const r=await fetch('/broker/properties/'+id+'/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok)load();else alert('Update failed');
}
function setPrice(id){const v=parseInt(document.getElementById('pr_'+id).value);if(v)upd(id,{price_inr:v});}
function setStatus(id,s){upd(id,{status:s});}
window.onload=()=>{const s=localStorage.getItem('btok');if(s){document.getElementById('tok').value=s;load();}};
</script></body></html>"""


# ── Telegram webhook (production mode) ───────────────────────────────────────

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        from telegram import Update
        from telegram.ext import Application
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not set")

        body = await request.json()
        app_tg = Application.builder().token(token).build()
        from interfaces.telegram_bot import (
            start, help_cmd, reset, profile_cmd, skip,
            handle_message, handle_voice, button_callback, shortlist_cmd,
        )
        from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, filters
        app_tg.add_handler(CommandHandler("start",     start))
        app_tg.add_handler(CommandHandler("help",      help_cmd))
        app_tg.add_handler(CommandHandler("reset",     reset))
        app_tg.add_handler(CommandHandler("profile",   profile_cmd))
        app_tg.add_handler(CommandHandler("skip",      skip))
        app_tg.add_handler(CommandHandler("shortlist", shortlist_cmd))
        app_tg.add_handler(MessageHandler(filters.VOICE, handle_voice))
        app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app_tg.add_handler(CallbackQueryHandler(button_callback))

        async with app_tg:
            update = Update.de_json(body, app_tg.bot)
            await app_tg.process_update(update)

        return {"ok": True}
    except Exception as e:
        logger.error(f"Telegram webhook error: {e}")
        return {"ok": False}


# ── WhatsApp webhook ───────────────────────────────────────────────────────────

@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    params = dict(request.query_params)
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "realestate_webhook_2026")
    if params.get("hub.verify_token") == verify_token:
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_inbound(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        entry   = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value   = changes.get("value", {})
        messages = value.get("messages", [])

        for msg in messages:
            msg_type = msg.get("type")
            sender   = msg.get("from")
            session_id = f"wa_{sender}"

            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
                if text:
                    background_tasks.add_task(_handle_whatsapp_message, sender, session_id, text)

        return {"ok": True}
    except Exception as e:
        logger.error(f"WhatsApp inbound error: {e}")
        return {"ok": True}


async def _handle_whatsapp_message(sender_phone: str, session_id: str, text: str):
    from notifications.whatsapp_notifier import _send
    # Check if this is a broker YES/NO reply to a pending availability confirmation FIRST.
    # If so, handle it and don't run it through the buyer-facing agent.
    try:
        from agent.broker_confirmation import handle_broker_reply
        if handle_broker_reply(sender_phone, text):
            return  # handled — don't process as a normal buyer message
    except Exception as e:
        logger.warning(f"broker_reply check failed: {e}")

    try:
        result = process_message(session_id=session_id, user_message=text, platform="whatsapp")
        _send(sender_phone, result["reply"])
    except Exception as e:
        logger.error(f"WhatsApp reply failed: {e}")
