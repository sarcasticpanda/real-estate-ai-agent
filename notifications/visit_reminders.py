"""
Visit reminders that actually fire — driven by the in-app scheduler (no external cron).

For every upcoming confirmed visit we send:
  • a day-of reminder (first scheduler tick on the visit day), and
  • a ~1-hour-before reminder,
to BOTH the customer (on their own channel — WhatsApp / Telegram / web) and the
broker (WhatsApp). Sent state is tracked per (meeting, stage) in a JSON file so
nothing is sent twice and it survives restarts.
"""

import os
import json
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_STORE = Path(__file__).resolve().parents[1] / "runtime" / "reminders_sent.json"
_LOCK = threading.Lock()


def _load() -> list:
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list) -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        _STORE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug(f"reminders_sent save failed: {e}")


def _time_str(dt: datetime) -> str:
    d = dt.astimezone(IST)
    h = d.hour % 12 or 12
    ap = "am" if d.hour < 12 else "pm"
    mm = f":{d.minute:02d}" if d.minute else ""
    return f"{h}{mm} {ap}"


def _full_str(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%a, %d %b") + f" at {_time_str(dt)}"


def run_visit_reminders() -> dict:
    """Send any day-of / 1-hour-before reminders now due. Safe to call every minute."""
    from database.supabase_client import get_upcoming_meetings, get_lead
    from notifications.whatsapp_notifier import _send
    from agent.broker_confirmation import notify_customer

    now = datetime.now(timezone.utc)
    try:
        meetings = get_upcoming_meetings(26) or []
    except Exception as e:
        logger.debug(f"reminders: get_upcoming_meetings failed: {e}")
        return {"sent": 0}

    bph = os.environ.get("BROKER_WHATSAPP_PHONE") or os.environ.get("WHATSAPP_BROKER_PHONE")
    count = 0
    with _LOCK:
        sent = set(_load())
        changed = False
        for m in meetings:
            if (m.get("status") or "").lower() == "cancelled":
                continue
            raw = m.get("scheduled_at")
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            mins = (dt - now).total_seconds() / 60.0
            if mins <= 0:
                continue

            now_ist = now.astimezone(IST)
            dt_ist = dt.astimezone(IST)
            stages = []
            # Morning-of "good morning" reminder: only during real morning hours (7–11am IST)
            # and only for visits still comfortably ahead (skips 3am spam and imminent visits,
            # which the 1-hour reminder already covers).
            if now_ist.date() == dt_ist.date() and 7 <= now_ist.hour < 11 and mins > 90:
                stages.append("day_of")
            if 0 < mins <= 65:
                stages.append("hour")
            if not stages:
                continue

            mid = m.get("id")
            lead = get_lead(m.get("lead_id")) or {}
            nm = lead.get("name") or "there"
            first = nm.split()[0] if nm != "there" else ""
            sid = lead.get("session_id")
            ph = lead.get("phone")
            t = _time_str(dt)
            full = _full_str(dt)

            for stage in stages:
                # Dedup by person + visit-day + stage (not meeting id) so duplicate meeting
                # rows for the same buyer/day can't produce duplicate reminders.
                who = (ph or m.get("lead_id") or mid)
                key = f"{who}:{dt_ist.date().isoformat()}:{stage}"
                if key in sent:
                    continue
                if stage == "day_of":
                    cust = (f"Good morning{(' ' + first) if first else ''}! 🌿 A quick reminder — "
                            f"your property visit is *today* at *{t}*. See you there!")
                    brok = f"Reminder — visit *today*: {nm} ({ph or ''}) at {full}."
                else:
                    cust = (f"Hi{(' ' + first) if first else ''}! Your property visit is in about "
                            f"*an hour*, at *{t}*. See you soon! 🌿")
                    brok = f"Visit in ~1 hour: {nm} ({ph or ''}) at {full}."
                try:
                    notify_customer(sid, ph, cust)
                except Exception as e:
                    logger.debug(f"reminder to customer failed: {e}")
                if bph:
                    try:
                        _send(bph, brok)
                    except Exception as e:
                        logger.debug(f"reminder to broker failed: {e}")
                sent.add(key)
                changed = True
                count += 1
        if changed:
            _save(list(sent)[-1000:])   # keep the file bounded
    return {"sent": count}
