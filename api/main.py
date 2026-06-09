"""
FastAPI application — the central API server.

Endpoints:
  POST /chat              — user sends a message, gets AI reply
  POST /upload            — broker uploads a CSV file
  GET  /properties        — list/search properties
  POST /webhook/n8n/lead  — n8n calls this after lead notification
  POST /webhook/n8n/meeting — n8n calls this after meeting scheduled

Run:
    uvicorn api.main:app --reload --port 8000
"""

import os
import sys
import logging
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from agent.property_agent import process_message
from broker.upload_handler import process_csv
from database.supabase_client import update_lead_status, save_meeting, get_upcoming_meetings

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
    meeting = save_meeting({
        "lead_id": payload.lead_id,
        "broker_id": payload.broker_id,
        "property_id": payload.property_id,
        "scheduled_at": payload.scheduled_at,
        "duration_minutes": payload.duration_minutes,
        "status": "confirmed",
    })
    return {"ok": True, "meeting_id": meeting.get("id")}


@app.get("/meetings/upcoming")
async def upcoming_meetings(hours_ahead: int = 24):
    """Get meetings in the next N hours (used by n8n reminder workflow)."""
    meetings = get_upcoming_meetings(hours_ahead)
    return {"count": len(meetings), "meetings": meetings}


# ── Simple web chat interface ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def web_chat():
    """Minimal chat UI served at root."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Real Estate AI Assistant</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f5f5f5; display: flex; justify-content: center; padding: 20px; }
    #app { width: 100%; max-width: 700px; display: flex; flex-direction: column; height: calc(100vh - 40px); }
    h1 { text-align: center; padding: 16px; color: #1a73e8; font-size: 20px; }
    #messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; background: white; border-radius: 12px; border: 1px solid #ddd; }
    .msg { max-width: 80%; padding: 12px 16px; border-radius: 18px; line-height: 1.5; white-space: pre-wrap; }
    .user { align-self: flex-end; background: #1a73e8; color: white; border-bottom-right-radius: 4px; }
    .assistant { align-self: flex-start; background: #f0f4ff; color: #222; border-bottom-left-radius: 4px; }
    #input-area { display: flex; gap: 8px; padding: 12px 0; }
    #input { flex: 1; padding: 12px 16px; border-radius: 24px; border: 1px solid #ddd; font-size: 15px; outline: none; }
    #input:focus { border-color: #1a73e8; }
    button { padding: 12px 24px; background: #1a73e8; color: white; border: none; border-radius: 24px; cursor: pointer; font-size: 15px; }
    button:hover { background: #1558b0; }
    .typing { color: #999; font-style: italic; font-size: 13px; }
  </style>
</head>
<body>
<div id="app">
  <h1>Real Estate AI Assistant — Lucknow</h1>
  <div id="messages">
    <div class="msg assistant">Hello! I can help you find properties in Lucknow. Tell me what you are looking for — BHK, area, budget, or any preferences like near metro or hospital.</div>
  </div>
  <div id="input-area">
    <input id="input" placeholder="e.g. 3 BHK flat in Gomti Nagar under 2 crore..." />
    <button onclick="sendMessage()">Send</button>
  </div>
</div>
<script>
  const SESSION_ID = 'web_' + Math.random().toString(36).slice(2, 10);
  const messagesEl = document.getElementById('messages');
  const inputEl = document.getElementById('input');

  inputEl.addEventListener('keydown', e => { if (e.key === 'Enter') sendMessage(); });

  async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = '';
    appendMessage(text, 'user');
    const typing = appendMessage('Typing...', 'assistant typing');
    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: SESSION_ID, message: text, platform: 'web' })
      });
      const data = await res.json();
      typing.remove();
      appendMessage(data.reply, 'assistant');
    } catch (e) {
      typing.textContent = 'Error: could not reach server.';
      typing.className = 'msg assistant';
    }
  }

  function appendMessage(text, cls) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    el.textContent = text;
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
