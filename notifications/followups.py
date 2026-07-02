"""
Broker follow-up reminders.

When the broker tells a customer they'll do something at a time ("I'll call you
this evening"), we schedule a reminder that pings the BROKER on WhatsApp at that
time so they actually follow through. Also lets the customer re-request.

Zero-config: reminders are persisted to a JSON file on the server and fired by an
in-app scheduler loop (see api.main startup) every minute — no external cron needed.
"""

import json
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_STORE = Path(__file__).resolve().parents[1] / "runtime" / "followups.json"
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
        logger.warning(f"followups save failed: {e}")


def add_followup(due: datetime, broker_phone: str, customer_name: str,
                 customer_phone: str, customer_session: str, note: str) -> None:
    """Schedule a reminder to the broker at `due` (aware datetime)."""
    if not due or not broker_phone:
        return
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    with _LOCK:
        items = _load()
        items.append({
            "due": due.astimezone(timezone.utc).isoformat(),
            "broker_phone": broker_phone,
            "customer_name": customer_name or "the customer",
            "customer_phone": customer_phone or "",
            "customer_session": customer_session or "",
            "note": note or "follow up",
            "sent": False,
            "created": datetime.now(timezone.utc).isoformat(),
        })
        _save(items)
    logger.info(f"Follow-up scheduled for {due.isoformat()} → broker {broker_phone}")


def run_due_followups() -> dict:
    """Send any reminders now due. Safe to call every minute."""
    from notifications.whatsapp_notifier import _send
    now = datetime.now(timezone.utc)
    sent = 0
    with _LOCK:
        items = _load()
        changed = False
        for it in items:
            if it.get("sent"):
                continue
            try:
                due = datetime.fromisoformat(it["due"])
            except Exception:
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due <= now:
                nm = it.get("customer_name") or "the customer"
                ph = it.get("customer_phone") or ""
                note = it.get("note") or "follow up with them"
                msg = (f"⏰ *Reminder*\nYou planned to {note} — *{nm}*"
                       + (f" ({ph})" if ph else "") + ".\n\n"
                       f"Reply *message {nm}: …* and I'll text them for you, "
                       f"or just give them a call.")
                try:
                    if _send(it["broker_phone"], msg):
                        it["sent"] = True
                        changed = True
                        sent += 1
                except Exception as e:
                    logger.warning(f"followup send failed: {e}")
        # Prune reminders that were sent more than 2 days ago to keep the file small.
        cutoff = now - timedelta(days=2)
        kept = []
        for it in items:
            if it.get("sent"):
                try:
                    if datetime.fromisoformat(it["due"]).replace(tzinfo=timezone.utc) < cutoff:
                        continue
                except Exception:
                    pass
            kept.append(it)
        if changed or len(kept) != len(items):
            _save(kept)
    return {"sent": sent}
