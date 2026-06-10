"""
FastAPI application — the central API server.

Endpoints:
  POST /chat              — user sends a message, gets AI reply
  POST /upload            — broker uploads a CSV file
  GET  /properties        — list/search properties
  POST /webhook/n8n/lead  — n8n calls this after lead notification
  POST /webhook/n8n/meeting — n8n calls this after meeting scheduled
  POST /webhook/telegram  — Telegram updates (webhook mode when hosted)
  GET  /webhook/whatsapp  — Meta webhook verification
  POST /webhook/whatsapp  — inbound WhatsApp messages

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
from broker.upload_handler import process_csv
from database.supabase_client import (
    update_lead_status, save_meeting, get_upcoming_meetings,
    upload_property_image, add_image_url_to_property, get_property_images,
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


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Send a message to the property AI assistant."""
    try:
        reply = process_message(
            session_id=req.session_id,
            user_message=req.message,
            platform=req.platform,
        )
        return ChatResponse(session_id=req.session_id, reply=reply)
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Broker upload endpoint ────────────────────────────────────────────────────

@app.post("/upload")
async def upload_properties(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    broker_id: str = Form(None),
):
    """
    Broker uploads a CSV file. Processing runs in the background.
    Returns immediately with a job acknowledgement.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted")

    # Save uploaded file to temp location
    content = await file.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="wb")
    tmp.write(content)
    tmp.close()

    background_tasks.add_task(_run_upload, tmp.name, broker_id)

    return {
        "status": "processing",
        "message": f"File '{file.filename}' received. Properties will be enriched and added to the knowledge base.",
        "broker_id": broker_id,
    }


def _run_upload(filepath: str, broker_id: str | None) -> None:
    try:
        result = process_csv(filepath, broker_id=broker_id)
        logger.info(f"Upload job complete: {result}")
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
    """Search properties using semantic query + filters."""
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
    """n8n calls this to update lead status after broker responds."""
    update_lead_status(payload.lead_id, payload.status, payload.notes)
    return {"ok": True}


class MeetingWebhook(BaseModel):
    lead_id: str
    broker_id: str
    property_id: str
    scheduled_at: str  # ISO datetime string
    duration_minutes: int = 60


@app.post("/webhook/n8n/meeting")
async def meeting_webhook(payload: MeetingWebhook):
    """n8n calls this after broker confirms a meeting slot."""
    from database.supabase_client import get_client

    meeting = save_meeting({
        "lead_id": payload.lead_id,
        "broker_id": payload.broker_id,
        "property_id": payload.property_id,
        "scheduled_at": payload.scheduled_at,
        "duration_minutes": payload.duration_minutes,
        "status": "confirmed",
    })

    # Create Google Calendar event (non-blocking — failure doesn't affect response)
    calendar_link = None
    try:
        from notifications.calendar_integration import send_calendar_invite

        client = get_client()
        lead_rows = client.table("leads").select("*").eq("id", payload.lead_id).execute()
        broker_rows = client.table("brokers").select("*").eq("id", payload.broker_id).execute()
        lead = lead_rows.data[0] if lead_rows.data else {}
        broker = broker_rows.data[0] if broker_rows.data else {}

        calendar_link = send_calendar_invite(meeting, lead, broker)
        if calendar_link:
            logger.info(f"Calendar event created: {calendar_link}")
    except Exception as e:
        logger.warning(f"Calendar integration skipped: {e}")

    return {"ok": True, "meeting_id": meeting.get("id"), "calendar_link": calendar_link}


@app.get("/meetings/upcoming")
async def upcoming_meetings(hours_ahead: int = 24):
    """Get meetings in the next N hours (used by n8n reminder workflow)."""
    meetings = get_upcoming_meetings(hours_ahead)
    return {"count": len(meetings), "meetings": meetings}


# ── Property image upload ─────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_SIZE_MB = 5


@app.post("/properties/{property_id}/images")
async def upload_image(property_id: str, file: UploadFile = File(...)):
    """
    Broker uploads a property image.
    Stores in Supabase Storage bucket 'property-images'.
    Bucket must be created in Supabase dashboard and set to public.
    """
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

    return {
        "ok": True,
        "property_id": property_id,
        "image_url": public_url,
        "filename": filename,
    }


@app.get("/properties/{property_id}/images")
async def get_images(property_id: str):
    """Get all image URLs for a property."""
    images = get_property_images(property_id)
    return {"property_id": property_id, "count": len(images), "images": images}


# ── Simple web chat interface ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def web_chat():
    """Chat UI with markdown rendering and image support."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Riya — Real Estate AI Lucknow</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f4f8; display: flex; justify-content: center; padding: 16px; }
    #app { width: 100%; max-width: 720px; display: flex; flex-direction: column; height: calc(100vh - 32px); }
    #header { text-align: center; padding: 14px; }
    #header h1 { color: #1a73e8; font-size: 22px; }
    #header p { color: #666; font-size: 13px; margin-top: 4px; }
    #messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; background: white; border-radius: 16px; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
    .msg { max-width: 85%; padding: 12px 16px; border-radius: 18px; line-height: 1.6; }
    .msg b, .msg strong { font-weight: 700; }
    .user { align-self: flex-end; background: #1a73e8; color: white; border-bottom-right-radius: 4px; }
    .assistant { align-self: flex-start; background: #f0f4ff; color: #1a1a2e; border-bottom-left-radius: 4px; }
    .assistant ul { padding-left: 20px; margin: 6px 0; }
    .assistant li { margin: 3px 0; }
    .prop-images { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
    .prop-images img { width: 110px; height: 80px; object-fit: cover; border-radius: 8px; cursor: pointer; border: 2px solid #e0e0e0; }
    .prop-images img:hover { border-color: #1a73e8; transform: scale(1.05); }
    #input-area { display: flex; gap: 8px; padding: 12px 0; }
    #input { flex: 1; padding: 12px 18px; border-radius: 24px; border: 1.5px solid #ddd; font-size: 15px; outline: none; background: white; }
    #input:focus { border-color: #1a73e8; box-shadow: 0 0 0 3px rgba(26,115,232,0.15); }
    #send { padding: 12px 24px; background: #1a73e8; color: white; border: none; border-radius: 24px; cursor: pointer; font-size: 15px; font-weight: 600; }
    #send:hover { background: #1558b0; }
    #send:disabled { background: #aaa; cursor: default; }
    .typing-dots { display: inline-block; color: #999; font-style: italic; }
    .lightbox { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); justify-content: center; align-items: center; z-index: 1000; cursor: pointer; }
    .lightbox img { max-width: 90vw; max-height: 90vh; border-radius: 8px; }
  </style>
</head>
<body>
<div id="lightbox" class="lightbox" onclick="this.style.display='none'">
  <img id="lightbox-img" src="" alt="Property photo"/>
</div>
<div id="app">
  <div id="header">
    <h1>Riya — Real Estate AI</h1>
    <p>Find your dream home in Lucknow</p>
  </div>
  <div id="messages">
    <div class="msg assistant">Namaste! I'm Riya, your personal property assistant for Lucknow. 🏠<br><br>Tell me what you're looking for — BHK, area, budget, or anything specific like near metro or hospital!</div>
  </div>
  <div id="input-area">
    <input id="input" placeholder="e.g. 3 BHK in Gomti Nagar under 1.5 crore near metro..." />
    <button id="send" onclick="sendMessage()">Send</button>
  </div>
</div>
<script>
  const SESSION_ID = 'web_' + Math.random().toString(36).slice(2, 10);
  const messagesEl = document.getElementById('messages');
  const inputEl = document.getElementById('input');
  const sendBtn = document.getElementById('send');

  inputEl.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) sendMessage(); });

  async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || sendBtn.disabled) return;
    inputEl.value = '';
    sendBtn.disabled = true;
    appendText(text, 'user');
    const typingEl = appendText('Typing...', 'assistant typing-dots');
    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: SESSION_ID, message: text, platform: 'web' })
      });
      const data = await res.json();
      typingEl.remove();
      appendRich(data.reply, 'assistant');
    } catch (e) {
      typingEl.textContent = 'Connection error. Please try again.';
      typingEl.className = 'msg assistant';
    } finally {
      sendBtn.disabled = false;
      inputEl.focus();
    }
  }

  function appendText(text, cls) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    el.textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function appendRich(text, cls) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    // Extract image URLs (https://...jpg/png/webp)
    const imgRegex = /https?:\\/\\/[^\\s]+\\.(?:jpg|jpeg|png|webp)(?:\\?[^\\s]*)*/gi;
    const urls = text.match(imgRegex) || [];
    const cleanText = text.replace(imgRegex, '').trim();
    // Simple markdown: **bold**, * list, newlines
    el.innerHTML = cleanText
      .replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>')
      .replace(/^\\* (.+)$/gm, '<li>$1</li>')
      .replace(/(<li>.*<\\/li>)/s, '<ul>$1</ul>')
      .replace(/\\n/g, '<br>');
    if (urls.length > 0) {
      const gallery = document.createElement('div');
      gallery.className = 'prop-images';
      urls.forEach(url => {
        const img = document.createElement('img');
        img.src = url;
        img.alt = 'Property photo';
        img.onclick = () => {
          document.getElementById('lightbox-img').src = url;
          document.getElementById('lightbox').style.display = 'flex';
        };
        gallery.appendChild(img);
      });
      el.appendChild(gallery);
    }
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }
</script>
</body>
</html>"""


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Telegram webhook (production mode) ───────────────────────────────────────

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """
    Receives Telegram updates when the bot runs in webhook mode (hosted).
    Set with: POST https://api.telegram.org/bot<TOKEN>/setWebhook?url=<PUBLIC_URL>/webhook/telegram
    """
    try:
        from telegram import Update
        from telegram.ext import Application
        import json

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not set")

        body = await request.json()
        # Process update via the shared bot application
        app_tg = Application.builder().token(token).build()
        # Register handlers (same as polling bot)
        from interfaces.telegram_bot import (
            start, help_cmd, reset, profile_cmd, skip,
            handle_message, handle_voice, button_callback,
        )
        from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, filters
        app_tg.add_handler(CommandHandler("start",   start))
        app_tg.add_handler(CommandHandler("help",    help_cmd))
        app_tg.add_handler(CommandHandler("reset",   reset))
        app_tg.add_handler(CommandHandler("profile", profile_cmd))
        app_tg.add_handler(CommandHandler("skip",    skip))
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


# ── WhatsApp webhook (inbound messages) ───────────────────────────────────────

@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    """Meta calls this GET to verify the webhook. Must return hub.challenge."""
    params = dict(request.query_params)
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "realestate_webhook_2026")
    if params.get("hub.verify_token") == verify_token:
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_inbound(request: Request, background_tasks: BackgroundTasks):
    """
    Receives inbound WhatsApp messages from Meta.
    Passes them through the same AI agent as Telegram/web.
    """
    try:
        body = await request.json()
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        for msg in messages:
            msg_type = msg.get("type")
            sender   = msg.get("from")   # e.g. "919936659513"
            session_id = f"wa_{sender}"

            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
                if text:
                    background_tasks.add_task(_handle_whatsapp_message, sender, session_id, text)

        return {"ok": True}
    except Exception as e:
        logger.error(f"WhatsApp inbound error: {e}")
        return {"ok": True}  # always 200 to Meta or it retries


async def _handle_whatsapp_message(sender_phone: str, session_id: str, text: str):
    """Process inbound WhatsApp message and send reply."""
    from notifications.whatsapp_notifier import _send
    try:
        reply = process_message(session_id=session_id, user_message=text, platform="whatsapp")
        _send(sender_phone, reply)
    except Exception as e:
        logger.error(f"WhatsApp reply failed: {e}")
