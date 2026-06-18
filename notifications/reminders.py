"""
Visit reminders — sends a day-before reminder for upcoming site visits.

Designed to be triggered once or twice a day by a free scheduler (Railway cron,
cron-job.org, n8n, or a manual call) via POST /cron/send-reminders.

Reminders go out over Gmail SMTP (free). The buyer is reminded if we have their
email; the broker is always reminded if their email is on file. Each meeting is
marked reminded so it isn't sent twice.
"""

import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.supabase_client import (
    get_upcoming_meetings, update_meeting, get_lead, get_broker_for_area, get_client,
)
from notifications.email_notifier import _send

logger = logging.getLogger(__name__)


def _when(meeting: dict) -> str:
    from datetime import datetime
    raw = meeting.get("scheduled_at")
    if not raw:
        return "soon (time to be confirmed)"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        h12 = dt.hour % 12 or 12
        ap = "am" if dt.hour < 12 else "pm"
        return dt.strftime("%A, %d %b") + f" at {h12} {ap}"
    except Exception:
        return str(raw)


def send_due_reminders(hours_ahead: int = 24) -> dict:
    """Send reminders for meetings within the next N hours that haven't been reminded."""
    summary = {"checked": 0, "buyer_sent": 0, "broker_sent": 0, "skipped": 0, "errors": 0}
    try:
        meetings = get_upcoming_meetings(hours_ahead)
    except Exception as e:
        logger.error(f"get_upcoming_meetings failed: {e}")
        return {**summary, "errors": 1}

    for m in meetings:
        summary["checked"] += 1
        if m.get("customer_reminded") and m.get("broker_reminded"):
            summary["skipped"] += 1
            continue

        lead = get_lead(m.get("lead_id")) or {}
        name = lead.get("name") or "there"
        area = lead.get("preferred_area") or lead.get("preferred_city") or "Lucknow"
        when = _when(m)
        prop = m.get("property_id") or ""

        # Buyer reminder (only if we have their email)
        if not m.get("customer_reminded") and lead.get("email"):
            try:
                subj = f"Reminder: your property visit {when} — Riya"
                body = (f"Hi {name},\n\nThis is a friendly reminder about your property visit on "
                        f"{when} in {area}{(' (ref ' + prop + ')') if prop else ''}.\n\n"
                        "Our consultant will be in touch to confirm the exact details. "
                        "See you there!\n\n— Riya, your property assistant")
                if _send(lead["email"], subj, body.replace("\n", "<br>"), body):
                    summary["buyer_sent"] += 1
            except Exception as e:
                logger.error(f"buyer reminder failed: {e}")
                summary["errors"] += 1

        # Broker reminder
        if not m.get("broker_reminded"):
            broker = get_broker_for_area(area) or {}
            if broker.get("email"):
                try:
                    subj = f"Visit reminder: {name} on {when}"
                    body = (f"Visit reminder\n\nLead: {name} ({lead.get('phone','')})\n"
                            f"When: {when}\nArea: {area}\nProperty: {prop or '—'}\n\n"
                            "Please confirm with the buyer.")
                    if _send(broker["email"], subj, body.replace("\n", "<br>"), body):
                        summary["broker_sent"] += 1
                except Exception as e:
                    logger.error(f"broker reminder failed: {e}")
                    summary["errors"] += 1

        try:
            update_meeting(m["id"], {"customer_reminded": True, "broker_reminded": True})
        except Exception as e:
            logger.error(f"mark reminded failed: {e}")

    logger.info(f"Reminders: {summary}")
    return summary


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(send_due_reminders())
