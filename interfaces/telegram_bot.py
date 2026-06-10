"""
Telegram bot interface for the Real Estate AI Agent.
Bot: @helper_panda_realesatet2bot

Features:
- Interactive onboarding (name → phone → email) on first /start
- Returning user greeted by name, session resumes automatically
- Voice message support (Groq Whisper — free tier, supports Hindi)
- Natural language property search
- Inline WhatsApp + Telegram call buttons to broker (free, no Twilio)
- Lead capture pre-filled from user profile (no repeated form filling)
- Property visit scheduling via n8n

Run:
    python interfaces/telegram_bot.py
"""

import os
import re
import sys
import logging
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from agent.property_agent import process_message
from database.supabase_client import get_client, get_user_profile, save_user_profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Onboarding state constants ────────────────────────────────────────────────
STEP_NAME  = "waiting_name"
STEP_PHONE = "waiting_phone"
STEP_EMAIL = "waiting_email"
STEP_DONE  = "complete"

PHONE_RE = re.compile(r"^(?:\+91[-\s]?)?[6-9]\d{9}$")


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    profile = get_user_profile(session_id)

    if profile.get("onboarding_step") == STEP_DONE:
        name = profile.get("name", "")
        await update.message.reply_text(
            f"Welcome back, *{name}!* 🏠\n\n"
            "Tell me what you're looking for and I'll find the best matches:\n"
            "_Example: 3 BHK in Gomti Nagar under 1.5 crore near metro_",
            parse_mode="Markdown",
        )
    else:
        save_user_profile(session_id, {"onboarding_step": STEP_NAME})
        await update.message.reply_text(
            "Namaste! 🙏 I'm *Riya*, your personal property assistant for Lucknow.\n\n"
            "I'll help you find the perfect home. First, may I know your *name*?",
            parse_mode="Markdown",
        )


# ── /help ─────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Commands:*\n"
        "/start — Start or resume your session\n"
        "/reset — Clear search history and start fresh\n"
        "/profile — View your saved profile\n"
        "/skip — Skip optional fields during setup\n"
        "/help — This message\n\n"
        "*Example queries:*\n"
        "• 3 BHK furnished flat in Aliganj under 1.5 crore\n"
        "• Near CMS school with gym and pool\n"
        "• Independent house Hazratganj with parking\n"
        "• Show me villas near Charbagh station\n\n"
        "🎤 You can also send *voice messages* — I understand Hindi too!",
        parse_mode="Markdown",
    )


# ── /reset ────────────────────────────────────────────────────────────────────

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    try:
        get_client().table("sessions").delete().eq("session_id", session_id).execute()
    except Exception:
        pass
    await update.message.reply_text(
        "Search history cleared. ✅\n\nWhat are you looking for?",
    )


# ── /profile ──────────────────────────────────────────────────────────────────

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    profile = get_user_profile(session_id)
    if profile.get("onboarding_step") != STEP_DONE:
        await update.message.reply_text("Profile not set up yet. Send /start to begin.")
        return
    await update.message.reply_text(
        f"*Your Profile*\n\n"
        f"👤 Name: {profile.get('name', '—')}\n"
        f"📱 Phone: {profile.get('phone', '—')}\n"
        f"📧 Email: {profile.get('email', '—')}\n\n"
        "_Your details are pre-filled when you express interest in a property._",
        parse_mode="Markdown",
    )


# ── /skip ─────────────────────────────────────────────────────────────────────

async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    profile = get_user_profile(session_id)
    step = profile.get("onboarding_step")

    if step == STEP_EMAIL:
        profile["onboarding_step"] = STEP_DONE
        save_user_profile(session_id, profile)
        name = profile.get("name", "")
        await update.message.reply_text(
            f"No problem, {name}! 🏠\n\n"
            "Now tell me what you're looking for:\n"
            "_Example: 3 BHK in Gomti Nagar under 1.5 crore_",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("Nothing to skip right now.")


# ── Shared message processor (used by text + voice handlers) ─────────────────

async def _process_search_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str,
    text: str,
) -> None:
    """Run property search and reply. Shared by text and voice handlers."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = process_message(session_id=session_id, user_message=text, platform="telegram")
    broker_buttons = _get_broker_buttons(session_id)
    if broker_buttons:
        await update.message.reply_text(reply, reply_markup=broker_buttons, parse_mode="Markdown")
    else:
        await update.message.reply_text(reply, parse_mode="Markdown")


# ── Text message handler ──────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    text = update.message.text.strip()
    profile = get_user_profile(session_id)
    step = profile.get("onboarding_step", STEP_NAME)

    # ── Onboarding: collect name ──────────────────────────────────────────────
    if step == STEP_NAME:
        if len(text) < 2 or not any(c.isalpha() for c in text):
            await update.message.reply_text("Please enter your name (at least 2 letters).")
            return
        profile["name"] = text.title()
        profile["onboarding_step"] = STEP_PHONE
        save_user_profile(session_id, profile)
        await update.message.reply_text(
            f"Nice to meet you, *{profile['name']}!* 😊\n\n"
            "What's your *mobile number*? _(10-digit Indian number)_",
            parse_mode="Markdown",
        )
        return

    # ── Onboarding: collect phone ─────────────────────────────────────────────
    if step == STEP_PHONE:
        cleaned = re.sub(r"[\s\-()]", "", text)
        if not PHONE_RE.match(cleaned) and not re.match(r"^[6-9]\d{9}$", cleaned):
            await update.message.reply_text(
                "Please enter a valid 10-digit Indian mobile number.\n"
                "_Example: 9876543210_",
                parse_mode="Markdown",
            )
            return
        profile["phone"] = cleaned.lstrip("+91").lstrip("91") if len(cleaned) > 10 else cleaned
        profile["onboarding_step"] = STEP_EMAIL
        save_user_profile(session_id, profile)
        await update.message.reply_text(
            "Got it! 📱\n\n"
            "What's your *email address*? _(for property brochures and visit confirmations)_\n"
            "_Type /skip to skip this._",
            parse_mode="Markdown",
        )
        return

    # ── Onboarding: collect email ─────────────────────────────────────────────
    if step == STEP_EMAIL:
        if "@" not in text or "." not in text.split("@")[-1]:
            await update.message.reply_text(
                "Please enter a valid email address.\n"
                "_Type /skip to skip this._",
                parse_mode="Markdown",
            )
            return
        profile["email"] = text.lower()
        profile["onboarding_step"] = STEP_DONE
        save_user_profile(session_id, profile)
        await update.message.reply_text(
            f"Perfect, *{profile.get('name', '')}!* You're all set. 🎉\n\n"
            "Now tell me what you're looking for:\n"
            "_Example: 3 BHK flat in Gomti Nagar under 1.5 crore near metro_",
            parse_mode="Markdown",
        )
        return

    # ── Normal conversation flow ──────────────────────────────────────────────
    await _process_search_message(update, context, session_id, text)


# ── Voice message handler ─────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe Telegram voice message via Groq Whisper and process as text."""
    session_id = str(update.effective_chat.id)
    profile = get_user_profile(session_id)

    if profile.get("onboarding_step") != STEP_DONE:
        await update.message.reply_text(
            "Please finish the quick setup first. Send /start to begin. 👋"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    transcript = await _transcribe_voice(update, context)
    if not transcript:
        await update.message.reply_text(
            "Sorry, I couldn't understand that voice message. 🎤\n"
            "Please try again or type your query."
        )
        return

    await update.message.reply_text(
        f"🎤 _I heard: \"{transcript}\"_",
        parse_mode="Markdown",
    )
    await _process_search_message(update, context, session_id, transcript)


async def _transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Download Telegram voice OGG → Groq Whisper → transcript string."""
    try:
        from groq import Groq
        groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

        voice_file = await update.message.voice.get_file()
        audio_bytes = await voice_file.download_as_bytearray()

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as f:
            result = groq_client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=f,
                language="hi",   # primary: Hindi; Whisper auto-detects English too
            )
        os.unlink(tmp_path)
        transcript = result.text.strip()
        logger.info(f"Voice transcript: {transcript!r}")
        return transcript or None

    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        return None


# ── Broker contact buttons ────────────────────────────────────────────────────

def _get_broker_buttons(session_id: str) -> InlineKeyboardMarkup | None:
    """
    After lead capture, return inline buttons for calling broker via WhatsApp and Telegram.
    FREE — no Twilio, no telephony API, just deep links.
    """
    try:
        result = (
            get_client()
            .table("leads")
            .select("broker_id, brokers(phone, telegram_chat_id)")
            .eq("session_id", session_id)
            .eq("status", "new")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None

        lead = result.data[0]
        broker = lead.get("brokers") or {}
        phone = broker.get("phone", "").replace("+91", "").replace(" ", "").replace("-", "")
        tg_id = broker.get("telegram_chat_id", "")

        buttons = []
        if phone:
            wa_url = f"https://wa.me/91{phone}?text=Hi%2C+I'm+interested+in+a+property+you+listed"
            buttons.append(InlineKeyboardButton("📱 WhatsApp Broker", url=wa_url))
        if tg_id and tg_id.startswith("@"):
            tg_url = f"https://t.me/{tg_id.lstrip('@')}"
            buttons.append(InlineKeyboardButton("✈️ Telegram Broker", url=tg_url))

        return InlineKeyboardMarkup([buttons]) if buttons else None

    except Exception as e:
        logger.debug(f"Could not get broker buttons: {e}")
        return None


# ── Callback for inline buttons ───────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "schedule_visit":
        await query.message.reply_text(
            "To schedule a visit, please share your preferred date and time. "
            "The broker will confirm the slot."
        )


# ── Bot entry point ───────────────────────────────────────────────────────────

def run_bot() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("skip", skip))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot @helper_panda_realesatet2bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
