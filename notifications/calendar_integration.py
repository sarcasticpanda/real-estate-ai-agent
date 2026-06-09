"""
Google Calendar integration — 100% free via Google Calendar API.

Two auth modes:
  1. OAuth 2.0 (for broker's personal calendar) — token saved locally
  2. Service Account (for a shared "Property Visits" calendar)

Setup (once per broker):
  1. Go to console.cloud.google.com → New Project → Enable Google Calendar API
  2. Create OAuth 2.0 credentials (type: Desktop) → download client_secret.json
  3. Place client_secret.json in project root
  4. Run:  python notifications/calendar_integration.py --setup
     → Opens browser for auth, saves token to calendar_token.json
  5. Add to .env:
     GOOGLE_CALENDAR_ID=primary   (or paste a specific calendar ID)

For service account (shared calendar):
  1. Create a Service Account in the same project
  2. Download service_account.json to project root
  3. Share the target calendar with the service account email
  4. Add to .env:  GOOGLE_CALENDAR_SERVICE_ACCOUNT=service_account.json
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

# Paths — credentials stay out of git (listed in .gitignore)
OAUTH_CLIENT_SECRET = ROOT / "client_secret.json"
OAUTH_TOKEN_FILE = ROOT / "calendar_token.json"
SERVICE_ACCOUNT_FILE = ROOT / os.environ.get("GOOGLE_CALENDAR_SERVICE_ACCOUNT", "service_account.json")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_service():
    """Return an authorized Google Calendar service object."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        from google.oauth2 import service_account
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        raise RuntimeError(
            "Google Calendar SDK not installed. Run:\n"
            "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )

    # Try service account first (server-to-server, no user interaction needed)
    if SERVICE_ACCOUNT_FILE.exists():
        creds = service_account.Credentials.from_service_account_file(
            str(SERVICE_ACCOUNT_FILE), scopes=SCOPES
        )
        return build("calendar", "v3", credentials=creds)

    # Fall back to OAuth 2.0 user flow
    creds = None
    if OAUTH_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(OAUTH_TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif OAUTH_CLIENT_SECRET.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            raise RuntimeError(
                "No Google Calendar credentials found.\n"
                "Place client_secret.json in the project root and run:\n"
                "  python notifications/calendar_integration.py --setup"
            )
        with open(OAUTH_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def create_site_visit_event(
    property_address: str,
    property_area: str,
    buyer_name: str,
    buyer_phone: str,
    broker_name: str,
    broker_phone: str,
    visit_datetime: datetime,
    duration_minutes: int = 60,
    broker_email: str | None = None,
) -> dict | None:
    """
    Create a Google Calendar event for a property site visit.
    Returns the created event dict (with htmlLink) or None on failure.
    """
    try:
        service = _get_service()
    except Exception as e:
        logger.warning(f"Google Calendar unavailable: {e}")
        return None

    end_dt = visit_datetime + timedelta(minutes=duration_minutes)

    event_body = {
        "summary": f"Site Visit — {property_area} ({buyer_name})",
        "description": (
            f"Property: {property_address}\n"
            f"Area: {property_area}\n\n"
            f"Buyer: {buyer_name}\n"
            f"Buyer Phone: {buyer_phone}\n\n"
            f"Broker: {broker_name}\n"
            f"Broker Phone: {broker_phone}\n\n"
            "This visit was scheduled by the Real Estate AI Agent."
        ),
        "start": {
            "dateTime": visit_datetime.isoformat(),
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": "Asia/Kolkata",
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 24 * 60},  # 1 day before
                {"method": "popup", "minutes": 60},        # 1 hour before
            ],
        },
    }

    # Add broker as attendee if email available
    if broker_email:
        event_body["attendees"] = [{"email": broker_email}]

    try:
        event = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_body,
            sendUpdates="all" if broker_email else "none",
        ).execute()
        logger.info(f"Calendar event created: {event.get('htmlLink')}")
        return event
    except Exception as e:
        logger.error(f"Calendar event creation failed: {e}")
        return None


def list_upcoming_visits(days_ahead: int = 7) -> list[dict]:
    """List site visit events in the next N days."""
    try:
        service = _get_service()
    except Exception as e:
        logger.warning(f"Google Calendar unavailable: {e}")
        return []

    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days_ahead)

    try:
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            q="Site Visit",
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return events_result.get("items", [])
    except Exception as e:
        logger.error(f"Failed to list calendar events: {e}")
        return []


def send_calendar_invite(meeting: dict, lead: dict, broker: dict) -> str | None:
    """
    High-level wrapper called from the property agent / n8n webhook.
    meeting: dict with scheduled_at (ISO string), duration_minutes
    lead: dict with name, phone, preferred_area, interested_property_id
    broker: dict with name, phone, email
    Returns event htmlLink or None.
    """
    scheduled_at = meeting.get("scheduled_at")
    if not scheduled_at:
        return None

    try:
        visit_dt = datetime.fromisoformat(scheduled_at)
    except ValueError:
        logger.error(f"Invalid scheduled_at format: {scheduled_at}")
        return None

    event = create_site_visit_event(
        property_address=lead.get("interested_property_id", "TBD"),
        property_area=lead.get("preferred_area", "Lucknow"),
        buyer_name=lead.get("name", "Buyer"),
        buyer_phone=lead.get("phone", ""),
        broker_name=broker.get("name", "Broker"),
        broker_phone=broker.get("phone", ""),
        visit_datetime=visit_dt,
        duration_minutes=meeting.get("duration_minutes", 60),
        broker_email=broker.get("email"),
    )
    return event.get("htmlLink") if event else None


# ── CLI setup helper ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--setup" in sys.argv:
        print("Running OAuth setup — a browser window will open...")
        svc = _get_service()
        print("Authentication successful! Token saved to calendar_token.json")
        print("You can now create calendar events from the agent.")
    else:
        print("Usage: python notifications/calendar_integration.py --setup")
