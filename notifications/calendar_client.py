"""
Google Calendar free/busy integration — 100% free via Service Account.

Setup (one-time, 10 minutes):
1. Go to https://console.cloud.google.com → New Project → Enable "Google Calendar API"
2. IAM & Admin → Service Accounts → Create → download JSON key → save as 'service_account.json'
3. Open Google Calendar → share your calendar with the service account email (give "Make changes" permission)
4. Set these in .env / Railway:
     GOOGLE_SERVICE_ACCOUNT_JSON = <contents of service_account.json as one-line JSON string>
     BROKER_GOOGLE_CALENDAR_ID   = your.email@gmail.com  (or the calendar ID from Calendar settings)

Without these env vars, all functions fall back gracefully (returns None / empty).
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
_CAL_ID  = os.environ.get("BROKER_GOOGLE_CALENDAR_ID", "primary")


def _get_service():
    """Build the Google Calendar service using the service account."""
    if not _SA_JSON:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        info = json.loads(_SA_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.warning(f"Google Calendar service unavailable: {e}")
        return None


def is_broker_free(dt: datetime, duration_minutes: int = 60) -> bool | None:
    """
    Check if the broker's Google Calendar is free at dt for duration_minutes.
    Returns:
      True  — slot is free
      False — slot is busy (event exists)
      None  — calendar not configured, assume free
    """
    svc = _get_service()
    if not svc:
        return None  # not configured — don't block

    try:
        start = dt.astimezone(timezone.utc).isoformat()
        end   = (dt + timedelta(minutes=duration_minutes)).astimezone(timezone.utc).isoformat()
        body  = {
            "timeMin": start, "timeMax": end,
            "timeZone": "Asia/Kolkata",
            "items": [{"id": _CAL_ID}],
        }
        resp = svc.freebusy().query(body=body).execute()
        busy = resp.get("calendars", {}).get(_CAL_ID, {}).get("busy", [])
        return len(busy) == 0  # True = free, False = busy
    except Exception as e:
        logger.warning(f"Google Calendar free/busy check failed: {e}")
        return None  # fail open


def add_event_to_broker_calendar(
    dt: datetime, summary: str, description: str = "", duration_minutes: int = 60
) -> str | None:
    """
    Create a Google Calendar event for the broker.
    Returns the event HTML link, or None on failure.
    """
    svc = _get_service()
    if not svc:
        return None

    try:
        start = dt.astimezone(timezone.utc)
        end   = start + timedelta(minutes=duration_minutes)
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
            "end":   {"dateTime": end.isoformat(),   "timeZone": "Asia/Kolkata"},
            "reminders": {"useDefault": False,
                          "overrides": [{"method": "popup", "minutes": 30}]},
        }
        created = svc.events().insert(calendarId=_CAL_ID, body=event).execute()
        link = created.get("htmlLink", "")
        logger.info(f"Calendar event created: {link}")
        return link
    except Exception as e:
        logger.warning(f"Could not create calendar event: {e}")
        return None


def get_broker_busy_slots(date: datetime, days: int = 7) -> list[dict]:
    """
    Return busy slots for the broker over the next N days.
    Each slot: {"start": ISO string, "end": ISO string}
    """
    svc = _get_service()
    if not svc:
        return []

    try:
        start = date.astimezone(timezone.utc)
        end   = (start + timedelta(days=days))
        body  = {
            "timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "timeZone": "Asia/Kolkata",
            "items": [{"id": _CAL_ID}],
        }
        resp = svc.freebusy().query(body=body).execute()
        return resp.get("calendars", {}).get(_CAL_ID, {}).get("busy", [])
    except Exception as e:
        logger.warning(f"Could not fetch busy slots: {e}")
        return []
