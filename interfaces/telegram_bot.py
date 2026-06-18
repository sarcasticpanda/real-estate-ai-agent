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

# Words that are not valid names
_NOT_A_NAME = {
    "hello", "hi", "hey", "hii", "hiii", "namaste", "yo", "yes", "no", "ok",
    "okay", "sup", "start", "none", "test", "bot", "riya", "agent", "help",
    "nope", "yep", "nah", "yeah", "sure", "fine", "good", "great", "nice",
}


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    profile = get_user_profile(session_id)

    import random
    greetings = [
        "Hello! I'm *Riya*, your property search assistant for Lucknow.\n\nI help buyers find the right home. Before we start — may I know your *name*?",
        "Hi there! I'm *Riya*, a property consultant for Lucknow. I'd love to help you find your perfect home!\n\nFirst, what should I call you?",
        "Welcome! I'm *Riya* — I specialize in finding properties in Lucknow that truly match what buyers need.\n\nTo get started, could you share your *name*?",
    ]
    returning_greetings = [
        "Welcome back, *{name}!* Great to hear from you again.\n\nTell me what you're looking for — budget, area, BHK, or anything specific.\n_Example: 3 BHK in Gomti Nagar under 1.5 crore_",
        "Hi *{name}*, good to have you back! What are we searching for today?\n_Example: 2 BHK under 50 lakh near metro_",
    ]

    if profile.get("onboarding_step") == STEP_DONE:
        name = profile.get("name", "")
        msg = random.choice(returning_greetings).format(name=name)
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        save_user_profile(session_id, {"onboarding_step": STEP_NAME})
        await update.message.reply_text(random.choice(greetings), parse_mode="Markdown")


# ── /help ─────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Commands*\n"
        "/start — Start or resume your session\n"
        "/shortlist — View your saved properties\n"
        "/reset — Clear search history and start fresh\n"
        "/setname YourName — Update your saved name\n"
        "/profile — View your saved contact details\n"
        "/skip — Skip the email field during setup\n"
        "/help — Show this message\n\n"
        "*Example searches*\n"
        "• 3 BHK furnished flat in Aliganj under 1.5 crore\n"
        "• Near CMS school with gym and swimming pool\n"
        "• Independent house in Hazratganj with parking\n"
        "• 2 BHK under 50 lakh near metro station\n\n"
        "You can also send *voice messages* in Hindi or English.",
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

async def setname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update stored name: /setname Rahul Sharma"""
    session_id = str(update.effective_chat.id)
    args = context.args
    if not args:
        await update.message.reply_text(
            "To update your name, use: `/setname YourName`\nExample: `/setname Rahul Sharma`",
            parse_mode="Markdown",
        )
        return
    new_name = " ".join(args).strip().title()
    if len(new_name) < 2 or not all(c.isalpha() or c.isspace() for c in new_name):
        await update.message.reply_text(
            "Please use a valid name (letters only).\nExample: `/setname Rahul Sharma`",
            parse_mode="Markdown",
        )
        return
    profile = get_user_profile(session_id)
    profile["name"] = new_name
    save_user_profile(session_id, profile)
    await update.message.reply_text(
        f"Done! I'll call you *{new_name}* from now on.",
        parse_mode="Markdown",
    )


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    profile = get_user_profile(session_id)
    step = profile.get("onboarding_step")

    if step == STEP_EMAIL:
        profile["onboarding_step"] = STEP_DONE
        save_user_profile(session_id, profile)
        name = profile.get("name", "")
        await update.message.reply_text(
            f"No problem, {name}!\n\n"
            "Tell me what you are looking for:\n"
            "_Example: 3 BHK in Gomti Nagar under 1.5 crore_",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("Nothing to skip right now.")


# ── Shared message processor (used by text + voice handlers) ─────────────────

async def _send_property_cards(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str,
    properties: list,
) -> None:
    """Send each property: photo(s) + a details message (with buttons) that ALWAYS
    appears — even when there's no photo — and survives Markdown-formatting errors."""
    from telegram.error import BadRequest
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")

    for i, prop in enumerate(properties, 1):
        images = prop.get("images", []) or []
        prop_id = prop.get("id", "")

        lines = [
            f"*Property {i}: {prop.get('bhk','')} BHK {prop.get('property_type','')}*",
            f"📍 {prop.get('area','')}  |  {prop.get('price_str','')}",
            f"📐 {prop.get('sqft','')} sqft  |  {prop.get('furnishing','')}",
        ]
        if prop.get("landmark_name") and prop.get("landmark_distance_km") is not None:
            lines.append(f"🎯 {prop['landmark_distance_km']} km from {prop['landmark_name']}")
        conn_str = " | ".join(f"{k}: {v}" for k, v in (prop.get("connectivity") or {}).items())
        if conn_str:
            lines.append(f"🏃 {conn_str}")
        if prop.get("top_amenities"):
            lines.append(f"✨ {', '.join(prop['top_amenities'][:4])}")
        if prop.get("map_url"):
            lines.append(f"🗺️ [View on map]({prop['map_url']})")
        for d in (prop.get("documents") or [])[:3]:
            lines.append(f"📄 [{d.get('label','Document')}]({d.get('url')})")
        caption = "\n".join(lines)

        buttons = [InlineKeyboardButton("📅 Book Visit", callback_data=f"visit_{i}")]
        if prop_id:
            buttons.append(InlineKeyboardButton("❤️ Save", callback_data=f"save_{prop_id}"))
        buttons.append(InlineKeyboardButton("📞 Callback", callback_data=f"call_{i}"))
        markup = InlineKeyboardMarkup([buttons])

        # Photos go first WITHOUT a caption, so a Markdown/photo failure can never hide
        # the property's details (which are sent as their own message below).
        try:
            if len(images) == 1:
                await context.bot.send_photo(chat_id=chat_id, photo=images[0])
            elif len(images) > 1:
                from telegram import InputMediaPhoto
                await context.bot.send_media_group(
                    chat_id=chat_id, media=[InputMediaPhoto(media=u) for u in images[:3]])
        except Exception as e:
            logger.warning(f"Property {i} photo send failed (continuing): {e}")

        # Details + buttons — try Markdown, fall back to plain text on a formatting error.
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=caption, parse_mode="Markdown",
                reply_markup=markup, disable_web_page_preview=True)
        except BadRequest:
            await context.bot.send_message(
                chat_id=chat_id, text=_strip_md(caption),
                reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"Property {i} details send failed: {e}")


def _strip_md(text: str) -> str:
    """Plain-text fallback: turn [label](url) into 'label: url' and drop * markers."""
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", text)
    return text.replace("*", "")


async def _process_search_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str,
    text: str,
) -> None:
    """Run property search and reply with text + property photo groups."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    result = process_message(session_id=session_id, user_message=text, platform="telegram")
    reply = result["reply"]
    properties = result.get("properties", [])

    await update.message.reply_text(reply, parse_mode="Markdown")

    if properties:
        await _send_property_cards(update, context, session_id, properties)


# ── Text message handler ──────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    text = update.message.text.strip()
    profile = get_user_profile(session_id)
    step = profile.get("onboarding_step", STEP_NAME)

    # ── Onboarding: collect name ──────────────────────────────────────────────
    if step == STEP_NAME:
        alpha_count = sum(1 for c in text if c.isalpha())
        has_digits = any(c.isdigit() for c in text)
        is_greeting = text.strip().lower() in _NOT_A_NAME
        if alpha_count < 3 or has_digits or is_greeting:
            await update.message.reply_text(
                "Please enter your full name (letters only, no numbers).\n"
                "_Example: Rahul Sharma, Priya Singh, Amit Kumar_",
                parse_mode="Markdown",
            )
            return
        import random
        profile["name"] = text.strip().title()
        profile["onboarding_step"] = STEP_PHONE
        save_user_profile(session_id, profile)
        name = profile["name"]
        phone_asks = [
            f"Lovely to meet you, *{name}!* May I have the privilege of your phone number? Our consultants will reach out only when you want them to.\n_(10-digit Indian mobile number)_",
            f"Great to meet you, *{name}!* Could I get your contact number? This helps me connect you with the right property consultant.\n_Example: 9876543210_",
            f"Nice to meet you, *{name}!* What's your mobile number? I'll use it only to have a consultant follow up when you're interested.",
        ]
        await update.message.reply_text(random.choice(phone_asks), parse_mode="Markdown")
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
        import random
        profile["phone"] = cleaned.lstrip("+91").lstrip("91") if len(cleaned) > 10 else cleaned
        profile["onboarding_step"] = STEP_EMAIL
        save_user_profile(session_id, profile)
        name = profile.get("name", "")
        email_asks = [
            f"Perfect! One last thing, *{name}* — could I have your *email address*? I'll send you property brochures and visit confirmations there.\n_Type /skip to skip._",
            f"Got it! And *{name}*, what's your *email address*? Useful for sending you detailed property documents.\n_Type /skip to skip._",
            f"Noted! Would you mind sharing your *email*? It helps us send you floor plans and confirm visit appointments.\n_Type /skip if you'd rather not._",
        ]
        await update.message.reply_text(random.choice(email_asks), parse_mode="Markdown")
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
        import random
        profile["email"] = text.lower()
        profile["onboarding_step"] = STEP_DONE
        save_user_profile(session_id, profile)
        name = profile.get("name", "")
        done_msgs = [
            f"All set, *{name}!* Now let's find you the perfect home.\n\nTell me what you're looking for — budget, area, BHK, any preferences.\n_Example: 3 BHK in Gomti Nagar under 1.5 crore near metro_",
            f"Wonderful, *{name}!* You're all set. What kind of property are you looking for?\n_Example: 2 BHK furnished flat under 60 lakh in Aliganj_",
        ]
        await update.message.reply_text(random.choice(done_msgs), parse_mode="Markdown")
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

async def shortlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/shortlist — show saved properties."""
    session_id = str(update.effective_chat.id)
    from agent.property_agent import process_message
    result = process_message(session_id=session_id, user_message="show my saved properties", platform="telegram")
    reply = result["reply"]
    properties = result.get("properties", [])
    await update.message.reply_text(reply, parse_mode="Markdown")
    if properties:
        await _send_property_cards(update, context, session_id, properties)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data and query.data.startswith("save_"):
        property_id = query.data[5:]
        session_id = str(query.message.chat.id)
        try:
            from database.supabase_client import get_session, save_session
            session = get_session(session_id)
            requirements = session.get("requirements") or {}
            shortlist = requirements.get("_shortlist") or []
            if property_id not in shortlist:
                shortlist.append(property_id)
                requirements["_shortlist"] = shortlist
                save_session(
                    session_id=session_id,
                    messages=session.get("messages") or [],
                    requirements=requirements,
                    stage=session.get("stage") or "discovery",
                )
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text("Property saved to your shortlist! Use /shortlist to view saved properties.")
            else:
                await query.answer("Already in your shortlist.", show_alert=False)
        except Exception as e:
            logger.error(f"Save to shortlist error: {e}")
            await query.answer("Could not save. Please try again.", show_alert=True)

    elif query.data and (query.data.startswith("visit_") or query.data.startswith("call_")):
        # Route the button through the agent so it triggers lead capture / scheduling.
        session_id = str(query.message.chat.id)
        num = query.data.split("_", 1)[1]
        is_visit = query.data.startswith("visit_")
        msg = (f"I want to visit property {num}" if is_visit
               else f"Please have someone call me back about property {num}")
        try:
            from agent.property_agent import process_message
            result = process_message(session_id=session_id, user_message=msg, platform="telegram")
            await query.edit_message_reply_markup(reply_markup=None)
            try:
                await query.message.reply_text(result["reply"], parse_mode="Markdown")
            except Exception:
                await query.message.reply_text(_strip_md(result["reply"]))
        except Exception as e:
            logger.error(f"visit/callback button error: {e}")
            await query.message.reply_text(
                "Let me connect you — could you share your name and mobile number?")


# ── Bot entry point ───────────────────────────────────────────────────────────

def run_bot() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      help_cmd))
    app.add_handler(CommandHandler("reset",     reset))
    app.add_handler(CommandHandler("profile",   profile_cmd))
    app.add_handler(CommandHandler("skip",      skip))
    app.add_handler(CommandHandler("setname",   setname))
    app.add_handler(CommandHandler("shortlist", shortlist_cmd))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot @helper_panda_realesatet2bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
