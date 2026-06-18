"""
Gmail SMTP email notifications — 100% free using Gmail App Passwords.

Setup:
1. Enable 2FA on your Google account
2. Go to Google Account → Security → App Passwords
3. Generate an app password for "Mail"
4. Add to .env:
   GMAIL_ADDRESS=youremail@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   (16-char app password, spaces OK)

Sends two emails on each new lead:
  - Broker: "New lead: Arjun Sharma (9876543210) interested in 3 BHK Gomti Nagar"
  - Buyer:  Warm confirmation from Riya with broker contact
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _get_credentials() -> tuple[str, str] | tuple[None, None]:
    addr = os.environ.get("GMAIL_ADDRESS")
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not addr or not pwd:
        return None, None
    return addr, pwd


def _send(to: str, subject: str, html_body: str, plain_body: str) -> bool:
    """Send one email. Returns True on success."""
    sender, password = _get_credentials()
    if not sender:
        logger.warning("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — email skipped")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Riya Real Estate <{sender}>"
    msg["To"] = to
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, to, msg.as_string())
        logger.info(f"Email sent to {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email failed to {to}: {e}")
        return False


def send_calendar_invite(to: str, subject: str, html_body: str, plain_body: str, ics_string: str) -> bool:
    """Send an email carrying a real calendar invite (.ics) so the buyer can add the visit
    to their own Google/Apple/Outlook calendar in one tap. Free via Gmail SMTP."""
    sender, password = _get_credentials()
    if not sender:
        logger.warning("GMAIL creds not set — calendar invite email skipped")
        return False
    from email.mime.base import MIMEBase
    from email import encoders

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = f"Riya Real Estate <{sender}>"
    msg["To"] = to

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_body, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    cal_part = MIMEText(ics_string, "calendar")
    cal_part.set_param("method", "REQUEST")
    alt.attach(cal_part)
    msg.attach(alt)

    ics_attach = MIMEBase("text", "calendar")
    ics_attach.set_payload(ics_string)
    encoders.encode_base64(ics_attach)
    ics_attach.add_header("Content-Disposition", "attachment; filename=property-visit.ics")
    msg.attach(ics_attach)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, to, msg.as_string())
        logger.info(f"Calendar invite sent to {to}")
        return True
    except Exception as e:
        logger.error(f"Calendar invite failed to {to}: {e}")
        return False


# ── Broker lead alert ──────────────────────────────────────────────────────────

def notify_broker_email(lead: dict, requirements: dict, broker_email: str | None) -> bool:
    """Send lead alert email to the broker."""
    if not broker_email:
        return False

    name = lead.get("name", "Unknown")
    phone = lead.get("phone", "N/A")
    area = requirements.get("area") or lead.get("preferred_area", "")
    bhk = requirements.get("bhk") or lead.get("preferred_bhk", "")
    budget = _fmt_budget(lead.get("budget_max"))

    subject = f"New Lead: {name} interested in {bhk} BHK {area}"

    plain = f"""New property lead received!

Customer: {name}
Phone: {phone}
Looking for: {bhk} BHK in {area}
Budget: {budget}
Status: New — please call within 2 hours

This lead was captured via the Real Estate AI Agent chat.
"""

    html = f"""
<html><body style="font-family:Arial,sans-serif;color:#333;max-width:600px;margin:auto">
<div style="background:#1a73e8;padding:20px;border-radius:8px 8px 0 0">
  <h2 style="color:white;margin:0">New Lead Alert</h2>
</div>
<div style="border:1px solid #e0e0e0;border-top:none;padding:24px;border-radius:0 0 8px 8px">
  <table style="width:100%;border-collapse:collapse">
    <tr><td style="padding:8px 0;font-weight:bold;width:140px">Customer</td>
        <td style="padding:8px 0">{name}</td></tr>
    <tr style="background:#f8f9fa"><td style="padding:8px;font-weight:bold">Phone</td>
        <td style="padding:8px"><a href="tel:{phone}" style="color:#1a73e8;font-size:18px;font-weight:bold">{phone}</a></td></tr>
    <tr><td style="padding:8px 0;font-weight:bold">Looking for</td>
        <td style="padding:8px 0">{bhk} BHK in {area}</td></tr>
    <tr style="background:#f8f9fa"><td style="padding:8px;font-weight:bold">Budget</td>
        <td style="padding:8px">{budget}</td></tr>
    <tr><td style="padding:8px 0;font-weight:bold">Status</td>
        <td style="padding:8px 0"><span style="background:#34a853;color:white;padding:3px 10px;border-radius:12px;font-size:13px">NEW</span></td></tr>
  </table>
  <div style="margin-top:20px;padding:16px;background:#fff3cd;border-radius:6px;border-left:4px solid #ffc107">
    <strong>Action required:</strong> Please call {name} at <strong>{phone}</strong> within 2 hours to arrange a site visit.
  </div>
  <p style="color:#999;font-size:12px;margin-top:24px">Sent by Real Estate AI Agent — automated lead capture</p>
</div>
</body></html>"""

    return _send(broker_email, subject, html, plain)


# ── Buyer confirmation ─────────────────────────────────────────────────────────

def notify_buyer_email(
    buyer_email: str | None,
    buyer_name: str,
    broker_name: str,
    broker_phone: str,
    area: str,
    bhk: str | int,
) -> bool:
    """Send confirmation email to the buyer."""
    if not buyer_email:
        return False

    subject = f"Your property inquiry for {bhk} BHK in {area} — Riya"

    plain = f"""Hi {buyer_name},

Shukriya for reaching out! Your inquiry has been noted and I've connected you with our broker.

Broker: {broker_name}
Phone: {broker_phone}

They will call you soon to arrange a visit. Meanwhile, feel free to message me any time on chat!

Warm regards,
Riya
Real Estate AI Assistant
"""

    html = f"""
<html><body style="font-family:Arial,sans-serif;color:#333;max-width:600px;margin:auto">
<div style="background:linear-gradient(135deg,#667eea,#764ba2);padding:24px;border-radius:8px 8px 0 0;text-align:center">
  <h2 style="color:white;margin:0">Your Dream Home Awaits!</h2>
  <p style="color:rgba(255,255,255,0.85);margin:8px 0 0">Riya Real Estate Assistant</p>
</div>
<div style="border:1px solid #e0e0e0;border-top:none;padding:28px;border-radius:0 0 8px 8px">
  <p style="font-size:16px">Hi <strong>{buyer_name}</strong>,</p>
  <p>Shukriya for your interest in a <strong>{bhk} BHK in {area}</strong>! 🎉</p>
  <p>I've connected you with our broker who will help you arrange a visit:</p>
  <div style="background:#f8f9fa;border-radius:8px;padding:16px;margin:16px 0;text-align:center">
    <p style="margin:0;font-size:14px;color:#666">Your Broker</p>
    <p style="margin:4px 0;font-size:22px;font-weight:bold;color:#333">{broker_name}</p>
    <a href="tel:{broker_phone}" style="font-size:20px;color:#1a73e8;font-weight:bold;text-decoration:none">{broker_phone}</a>
    <br/>
    <a href="https://wa.me/91{broker_phone.replace('+91','').replace(' ','')}"
       style="display:inline-block;margin-top:12px;background:#25d366;color:white;padding:8px 20px;border-radius:20px;text-decoration:none;font-size:14px">
      WhatsApp Broker
    </a>
  </div>
  <p style="color:#666;font-size:14px">They'll call you soon to schedule a visit at a time convenient for you.</p>
  <p style="color:#999;font-size:12px;margin-top:28px;border-top:1px solid #eee;padding-top:16px">
    This is an automated message from Real Estate AI Agent.
    You can continue chatting with Riya anytime for more property options.
  </p>
</div>
</body></html>"""

    return _send(buyer_email, subject, html, plain)


# ── Utility ────────────────────────────────────────────────────────────────────

def _fmt_budget(price_inr: int | None) -> str:
    if not price_inr:
        return "Not specified"
    if price_inr >= 10_000_000:
        return f"{price_inr / 10_000_000:.2g} Crore"
    if price_inr >= 100_000:
        return f"{price_inr / 100_000:.2g} Lakh"
    return f"Rs.{price_inr:,}"
