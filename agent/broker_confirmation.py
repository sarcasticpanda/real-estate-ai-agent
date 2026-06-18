"""
WhatsApp two-way broker confirmation flow.

Pipeline:
  1. Buyer books a visit → _ask_broker_availability() sends broker a WhatsApp:
       "Saubhagya Kashyap wants to visit [Property] on Saturday 21 Jun at 5 pm.
        Are you free? Reply YES to confirm or NO to suggest another time."
  2. Broker replies on WhatsApp → inbound webhook calls handle_broker_reply()
  3. YES  → save meeting, send .ics to both buyer + broker, update lead → 'scheduled'
     NO   → message the buyer: "Broker is busy that slot, please pick another time"
     reschedule text → ask broker for their available slot, relay back to buyer
"""

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_YES_RE = re.compile(r"^\s*(yes|ha|haan|yeah|yep|sure|ok|okay|confirm|confirmed|free|done|y)\s*[.!]*\s*$", re.I)
_NO_RE  = re.compile(r"^\s*(no|nope|nahi|na|busy|not free|can'?t|cannot|sorry|unavailable|n)\s*[.!]*\s*$", re.I)


def ask_broker_availability(
    buyer_name: str,
    buyer_phone: str,
    buyer_session_id: str,
    proposed_when: str,
    proposed_dt: datetime | None,
    property_id: str | None,
    lead_id: str | None,
    broker_phone: str,
) -> bool:
    """
    Send the broker a WhatsApp message asking if they're free for the proposed slot,
    and store a pending confirmation record so we can match their reply.
    Returns True if the message was sent.
    """
    from notifications.whatsapp_notifier import _send
    from database.supabase_client import save_broker_confirmation

    prop_label = f"Property ID {property_id}" if property_id else "the property"
    msg = (
        f"Hi! *{buyer_name}* wants to visit *{prop_label}* "
        f"on *{proposed_when}*.\n\n"
        f"Are you free at that time?\n"
        f"Reply *YES* to confirm or *NO* if busy.\n\n"
        f"Buyer's phone: {buyer_phone}"
    )

    sent = _send(broker_phone, msg)
    if not sent:
        logger.warning(f"Could not send broker confirmation WA to {broker_phone}")
        return False

    try:
        save_broker_confirmation({
            "broker_phone": broker_phone,
            "buyer_name": buyer_name,
            "buyer_phone": buyer_phone,
            "buyer_session_id": buyer_session_id,
            "property_id": property_id,
            "lead_id": lead_id,
            "proposed_dt": proposed_dt.isoformat() if proposed_dt else None,
            "proposed_when": proposed_when,
        })
    except Exception as e:
        logger.error(f"Could not save broker_confirmation record: {e}")

    logger.info(f"Broker availability check sent to {broker_phone} for {proposed_when}")
    return True


def handle_broker_reply(broker_phone: str, reply_text: str) -> bool:
    """
    Called when broker replies on WhatsApp. Matches to a pending confirmation,
    then either books the meeting or informs the buyer.
    Returns True if we handled it (so the general WA handler doesn't also process it).
    """
    from database.supabase_client import (
        get_pending_broker_confirmation, update_broker_confirmation,
        save_meeting, update_lead,
    )
    from notifications.whatsapp_notifier import _send
    from notifications.email_notifier import _send as _email_send
    from agent.property_agent import _build_ics, _gcal_link, _send_visit_confirmation_email

    conf = get_pending_broker_confirmation(broker_phone)
    if not conf:
        return False  # not a pending confirmation reply — let normal agent handle it

    conf_id   = conf["id"]
    buyer_phone   = conf.get("buyer_phone", "")
    buyer_name    = conf.get("buyer_name", "")
    buyer_sid     = conf.get("buyer_session_id", "")
    proposed_when = conf.get("proposed_when", "the agreed time")
    proposed_dt_s = conf.get("proposed_dt")
    property_id   = conf.get("property_id")
    lead_id       = conf.get("lead_id")

    proposed_dt = None
    if proposed_dt_s:
        try:
            proposed_dt = datetime.fromisoformat(proposed_dt_s)
        except Exception:
            pass

    if _YES_RE.match(reply_text.strip()):
        # Broker is free → book the meeting
        update_broker_confirmation(conf_id, "yes")

        meeting_fields = {
            "property_id": property_id,
            "status": "confirmed",
            "notes": f"Broker confirmed via WhatsApp for {proposed_when}",
        }
        if proposed_dt:
            meeting_fields["scheduled_at"] = proposed_dt.isoformat()
        if lead_id:
            meeting_fields["lead_id"] = lead_id

        try:
            save_meeting(meeting_fields)
            if lead_id:
                update_lead(lead_id, {"status": "visit"})
        except Exception as e:
            logger.error(f"save_meeting failed after broker YES: {e}")

        # WhatsApp the broker confirmation
        _send(broker_phone, f"Confirmed! I've booked the visit for *{proposed_when}*. The buyer will be informed.")

        # WhatsApp + email the buyer
        buyer_msg = (
            f"Great news, {buyer_name}! Your visit has been confirmed for *{proposed_when}*. "
            f"Our consultant will be there — see you!"
        )
        if buyer_phone:
            _send(buyer_phone, buyer_msg)

        # Email .ics invite to buyer if their session has an email
        if buyer_sid:
            try:
                from database.supabase_client import get_session
                sess = get_session(buyer_sid)
                buyer_email = ((sess.get("requirements") or {}).get("_profile") or {}).get("email")
                if buyer_email and proposed_dt:
                    gcal = _gcal_link(proposed_dt, "Property visit", f"Visit on {proposed_when}", "Lucknow")
                    _send_visit_confirmation_email(buyer_email, buyer_name, proposed_when,
                                                   "Lucknow", gcal, proposed_dt)
            except Exception as e:
                logger.warning(f"Could not send buyer email after broker YES: {e}")

        logger.info(f"Meeting confirmed by broker {broker_phone} for {proposed_when}")
        return True

    elif _NO_RE.match(reply_text.strip()):
        # Broker is busy → inform buyer, ask them to pick another time
        update_broker_confirmation(conf_id, "no")
        _send(broker_phone, "Got it — I'll let the buyer know and ask them to pick another time.")
        if buyer_phone:
            _send(buyer_phone,
                  f"Hi {buyer_name}, our consultant is busy at *{proposed_when}*. "
                  "Could you suggest another day/time? I'll check availability right away.")
        logger.info(f"Broker {broker_phone} declined {proposed_when}")
        return True

    # Any other text — broker might be suggesting an alternative or asking a question.
    # Don't consume it; let the normal agent handle it as a free-text conversation.
    return False
