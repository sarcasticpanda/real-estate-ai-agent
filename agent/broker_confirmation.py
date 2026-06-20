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
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_YES_RE        = re.compile(r"^\s*(yes|ha|haan|yeah|yep|sure|ok|okay|confirm|confirmed|free|done|y)\s*[.!]*\s*$", re.I)
_NO_RE         = re.compile(r"^\s*(no|nope|nahi|na|busy|not free|can'?t|cannot|sorry|unavailable|n)\s*[.!]*\s*$", re.I)
_RESCHEDULE_RE = re.compile(r"\b(reschedule|change.{0,10}time|different.{0,10}slot|new.{0,10}time|move.{0,10}to|shift.{0,10}to)\b", re.I)
_BROKER_PHONE  = os.environ.get("BROKER_WHATSAPP_PHONE", os.environ.get("WHATSAPP_BROKER_PHONE", ""))


def ask_broker_availability(
    buyer_name: str,
    buyer_phone: str,
    buyer_session_id: str,
    proposed_when: str,
    proposed_dt: datetime | None,
    property_id: str | None,
    lead_id: str | None,
    meeting_id: str | None,
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
            "meeting_id": meeting_id,
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
        save_meeting, update_meeting, update_lead,
    )
    from notifications.whatsapp_notifier import _send
    from notifications.email_notifier import _send as _email_send
    from agent.property_agent import _build_ics, _gcal_link, _send_visit_confirmation_email

    if _RESCHEDULE_RE.search(reply_text):
        return handle_broker_reschedule(broker_phone, reply_text)

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
    meeting_id    = conf.get("meeting_id")

    proposed_dt = None
    if proposed_dt_s:
        try:
            proposed_dt = datetime.fromisoformat(proposed_dt_s)
        except Exception:
            pass

    if _YES_RE.match(reply_text.strip()):
        # Broker is free → book the meeting
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
            if meeting_id:
                update_meeting(meeting_id, meeting_fields)
            else:
                saved = save_meeting(meeting_fields)
                meeting_id = (saved or {}).get("id")
                if meeting_id:
                    update_broker_confirmation(conf_id, fields={"meeting_id": meeting_id})
            if lead_id:
                update_lead(lead_id, {"status": "visit"})
            update_broker_confirmation(conf_id, "yes")
        except Exception as e:
            logger.error(f"save_meeting failed after broker YES: {e}")
            _send(broker_phone, "I couldn't confirm that visit right now. Please reply YES again shortly.")
            return True

        # WhatsApp the broker confirmation
        broker_gcal = _gcal_link(proposed_dt, "Property visit", f"Visit with {buyer_name}", "Lucknow")
        broker_calendar = f"\nAdd it to your calendar: {broker_gcal}" if broker_gcal else ""
        _send(broker_phone, f"Confirmed! I've booked the visit for *{proposed_when}*. "
                            f"The buyer will be informed.{broker_calendar}")
        _send_broker_calendar_invite(broker_phone, buyer_name, proposed_when, proposed_dt, broker_gcal)

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
        if meeting_id:
            update_meeting(meeting_id, {
                "status": "cancelled",
                "notes": f"Broker declined requested slot: {proposed_when}",
            })
        _prepare_buyer_whatsapp_reschedule(conf)
        _send(broker_phone, "Got it — I'll let the buyer know and ask them to pick another time.")
        if buyer_phone:
            _send(buyer_phone,
                  f"Hi {buyer_name}, our consultant is busy at *{proposed_when}*. "
                  "Could you suggest another day/time? I'll check availability right away.")
        logger.info(f"Broker {broker_phone} declined {proposed_when}")
        return True

    # Any other text — check if broker is trying to reschedule
    if _RESCHEDULE_RE.search(reply_text):
        return handle_broker_reschedule(broker_phone, reply_text, conf)

    # Otherwise don't consume it; let the normal agent handle it.
    return False


def handle_broker_reschedule(broker_phone: str, text: str, conf: dict | None = None) -> bool:
    """
    Broker texts something like "reschedule Arjun to Friday 4pm" or
    "change time to Thursday 6pm". We parse the new time, update the meeting,
    resend .ics to the buyer, and confirm to the broker.
    Returns True if we handled it.
    """
    from notifications.whatsapp_notifier import _send
    from database.supabase_client import (
        get_latest_broker_confirmation, update_broker_confirmation, update_meeting,
    )
    from agent.property_agent import _parse_visit_time, _build_ics, _gcal_link, _send_visit_confirmation_email

    # Try to find the most recent confirmed meeting for this broker
    if conf is None:
        phone_match = re.search(r"(?<!\d)(?:91)?([6-9]\d{9})(?!\d)", text)
        buyer_phone_hint = phone_match.group(1) if phone_match else None
        conf = get_latest_broker_confirmation(broker_phone, buyer_phone_hint)

    if not conf:
        # Try looking up any recent meeting associated with this broker
        # (fall back to a generic "tell me the buyer name / new time" prompt)
        _send(broker_phone,
              "Sure — to reschedule, please reply:\n"
              "*RESCHEDULE [buyer phone] to [new day and time]*\n"
              "e.g. RESCHEDULE 9876543210 to Friday 5pm")
        return True

    dt, when = _parse_visit_time(text)
    if not dt:
        _send(broker_phone,
              "I couldn't read the new time. Please reply like:\n"
              "*Thursday 4pm* or *25 Jun at 6 pm*")
        return True

    meeting_id = conf.get("meeting_id")
    buyer_phone = conf.get("buyer_phone","")
    buyer_name  = conf.get("buyer_name","the buyer")
    buyer_sid   = conf.get("buyer_session_id","")
    proposed_when = when

    if not meeting_id:
        _send(broker_phone, "I found the visit details but not its meeting record. Please reschedule it from the dashboard.")
        return True

    try:
        update_meeting(meeting_id, {
            "scheduled_at": dt.isoformat(),
            "status": "confirmed",
            "notes": f"Broker rescheduled via WhatsApp to {proposed_when}",
        })
        update_broker_confirmation(
            conf["id"], "rescheduled",
            {"proposed_dt": dt.isoformat(), "proposed_when": proposed_when},
        )
    except Exception as e:
        logger.error(f"update_meeting on reschedule: {e}")
        _send(broker_phone, "I couldn't save that new time. Please try again shortly.")
        return True

    # Notify buyer via WhatsApp
    if buyer_phone:
        _send(buyer_phone,
              f"Hi {buyer_name}, your property visit has been rescheduled to *{proposed_when}*. "
              "Same property — see you there!")

    # Email buyer new .ics if we have their email
    if buyer_sid:
        try:
            from database.supabase_client import get_session
            sess = get_session(buyer_sid)
            buyer_email = ((sess.get("requirements") or {}).get("_profile") or {}).get("email")
            if buyer_email:
                gcal = _gcal_link(dt, "Property visit", f"Rescheduled visit on {proposed_when}", "Lucknow")
                _send_visit_confirmation_email(buyer_email, buyer_name, proposed_when, "Lucknow", gcal, dt)
        except Exception as e:
            logger.warning(f"Could not email buyer on reschedule: {e}")

    broker_gcal = _gcal_link(dt, "Property visit", f"Visit with {buyer_name}", "Lucknow")
    _send(broker_phone,
          f"Done! Rescheduled to *{proposed_when}*. "
          f"{buyer_name} has been notified on WhatsApp.\n"
          f"Add it to your calendar: {broker_gcal}")
    logger.info(f"Broker {broker_phone} rescheduled meeting {meeting_id} to {proposed_when}")
    return True


def _prepare_buyer_whatsapp_reschedule(conf: dict) -> None:
    """Make the buyer's next WhatsApp message act as the replacement visit time."""
    buyer_phone = conf.get("buyer_phone")
    meeting_id = conf.get("meeting_id")
    if not buyer_phone or not meeting_id:
        return

    from database.supabase_client import get_session, save_session, _normalize_indian_phone

    session_id = f"wa_{_normalize_indian_phone(buyer_phone)}"
    session = get_session(session_id)
    requirements = session.get("requirements") or {}
    profile = requirements.get("_profile") or {}
    profile.update({"name": conf.get("buyer_name"), "phone": buyer_phone})
    requirements["_profile"] = profile
    requirements["_pending_meeting"] = {
        "meeting_id": meeting_id,
        "lead_id": conf.get("lead_id"),
        "property_id": conf.get("property_id"),
        "phone": buyer_phone,
        "name": conf.get("buyer_name"),
        "reschedule": True,
    }
    requirements["_last_meeting_id"] = meeting_id
    save_session(session_id, session.get("messages") or [], requirements, "scheduling")


def _send_broker_calendar_invite(broker_phone: str, buyer_name: str, when: str,
                                 dt: datetime | None, gcal: str | None) -> None:
    """Email the broker an ICS invite when their broker profile has an email."""
    if not dt:
        return
    try:
        from database.supabase_client import get_broker_by_phone
        from notifications.email_notifier import send_calendar_invite
        from agent.property_agent import _build_ics

        broker = get_broker_by_phone(broker_phone) or {}
        email = broker.get("email")
        if not email:
            return
        broker_name = broker.get("name") or "there"
        subject = f"Property visit with {buyer_name} - {when}"
        plain = (f"Hi {broker_name},\n\nYour property visit with {buyer_name} is confirmed "
                 f"for {when}.\n{('Add to Google Calendar: ' + gcal) if gcal else ''}")
        calendar_html = f'<p><a href="{gcal}">Add to Google Calendar</a></p>' if gcal else ""
        html = (f"<p>Hi {broker_name},</p><p>Your property visit with <b>{buyer_name}</b> "
                f"is confirmed for <b>{when}</b>.</p>{calendar_html}")
        ics = _build_ics(dt, f"Property visit with {buyer_name}",
                         "Broker-confirmed property visit", "Lucknow", email)
        send_calendar_invite(email, subject, html, plain, ics)
    except Exception as e:
        logger.warning(f"Could not send broker calendar invite: {e}")
