"""
Customer authentication for the web app.

Two login methods, both issuing a JWT session:
  1. Email OTP  — email → 6-digit code (Gmail SMTP) → verify → JWT
  2. Google     — Google Identity Services credential (ID token) → verify → JWT

Customers are stored in the `customers` table (Supabase). Favourites + booked
visits are tied to the customer so they can see everything they liked / scheduled.

Env:
  JWT_SECRET               signing secret for session tokens
  GOOGLE_OAUTH_CLIENT_ID   (optional) enables Google login
"""

import os
import time
import random
import logging
from datetime import datetime, timedelta, timezone

import jwt  # PyJWT

logger = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-insecure-change-me")
JWT_ALGO = "HS256"
JWT_TTL_DAYS = 30
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")

# In-memory OTP store (single uvicorn worker): email -> (code, expires_at)
_otp_store: dict[str, tuple[str, float]] = {}
_OTP_TTL = 600  # 10 minutes


# ── JWT sessions ──────────────────────────────────────────────────────────────

def issue_jwt(customer: dict) -> str:
    payload = {
        "sub": str(customer.get("id")),
        "email": customer.get("email"),
        "name": customer.get("name"),
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def verify_jwt(token: str) -> dict | None:
    if not token:
        return None
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except Exception:
        return None


# ── Email OTP ─────────────────────────────────────────────────────────────────

def request_otp(email: str) -> bool:
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    code = f"{random.randint(0, 999999):06d}"
    _otp_store[email] = (code, time.time() + _OTP_TTL)
    try:
        from notifications.email_notifier import _send
        html = (f"<div style='font-family:sans-serif'><p>Hi,</p>"
                f"<p>Your Riya login code is:</p>"
                f"<p style='font-size:28px;font-weight:700;letter-spacing:4px'>{code}</p>"
                f"<p style='color:#666'>It's valid for 10 minutes. If you didn't request this, ignore it.</p>"
                f"<p>— Riya, your property assistant 🏠</p></div>")
        _send(email, "Your Riya login code", html, f"Your Riya login code is {code} (valid 10 minutes).")
        return True
    except Exception as e:
        logger.error(f"OTP email failed: {e}")
        return False


def verify_otp(email: str, code: str) -> bool:
    email = (email or "").strip().lower()
    rec = _otp_store.get(email)
    if not rec:
        return False
    real, exp = rec
    if time.time() > exp:
        _otp_store.pop(email, None)
        return False
    if (code or "").strip() != real:
        return False
    _otp_store.pop(email, None)  # single-use
    return True


# ── Google Identity ───────────────────────────────────────────────────────────

def verify_google(credential: str) -> dict | None:
    """Verify a Google Identity Services credential (ID token). Returns profile dict."""
    if not GOOGLE_CLIENT_ID or not credential:
        return None
    try:
        from google.oauth2 import id_token as gid_token
        from google.auth.transport import requests as g_requests
        info = gid_token.verify_oauth2_token(credential, g_requests.Request(), GOOGLE_CLIENT_ID)
        if not info.get("email_verified", True):
            return None
        return {"email": info["email"], "name": info.get("name"), "sub": info.get("sub")}
    except Exception as e:
        logger.warning(f"Google token verify failed: {e}")
        return None


# ── Customer records ──────────────────────────────────────────────────────────

def get_or_create_customer(email: str, name: str | None = None, google_sub: str | None = None) -> dict:
    from database.supabase_client import get_client
    c = get_client()
    email = email.strip().lower()
    rows = c.table("customers").select("*").eq("email", email).limit(1).execute().data
    if rows:
        cust = rows[0]
        upd = {}
        if name and not cust.get("name"):
            upd["name"] = name
        if google_sub and not cust.get("google_sub"):
            upd["google_sub"] = google_sub
        if upd:
            c.table("customers").update(upd).eq("id", cust["id"]).execute()
            cust = {**cust, **upd}
        return cust
    saved = c.table("customers").insert(
        {"email": email, "name": name, "google_sub": google_sub, "favourites": []}
    ).execute().data
    return saved[0] if saved else {"email": email, "name": name, "favourites": []}


def get_customer(customer_id: str) -> dict | None:
    from database.supabase_client import get_client
    rows = get_client().table("customers").select("*").eq("id", customer_id).limit(1).execute().data
    return rows[0] if rows else None


def set_customer_favourites(customer_id: str, favourites: list[str]) -> None:
    from database.supabase_client import get_client
    get_client().table("customers").update({"favourites": favourites}).eq("id", customer_id).execute()


def set_customer_phone(customer_id: str, phone: str) -> None:
    from database.supabase_client import get_client
    get_client().table("customers").update({"phone": phone}).eq("id", customer_id).execute()
