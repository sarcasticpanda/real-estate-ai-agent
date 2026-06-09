"""
Telegram bot interface for the Real Estate AI Agent.
Bot: @helper_panda_realesatet2bot

Features:
- Natural language property search
- Inline WhatsApp + Telegram call buttons to broker (free, no Twilio)
- Lead capture
- Property visit scheduling via n8n

Run:
    python interfaces/telegram_bot.py
"""

import os
import sys
import logging
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Namaste! I can help you find properties in Lucknow.\n\n"
        "Just tell me what you are looking for:\n"
        "Example: *3 BHK flat in Gomti Nagar under 2 crore near metro*\n\n"
        "Type /help for more options.",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Commands:*\n"
        "/start - Start fresh\n"
        "/reset - Clear your search history\n"
        "/help - This message\n\n"
        "*Example queries:*\n"
        "- 3 BHK furnished flat in Aliganj under 1.5 crore\n"
        "- Near CMS school with gym and pool\n"
        "- Independent house Hazratganj with parking\n"
        "- Show me villas near Charbagh station",
        parse_mode="Markdown",
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear session so user can start a fresh search."""
    session_id = str(update.effective_chat.id)
    try:
        from database.supabase_client import get_client
        get_client().table("sessions").delete().eq("session_id", session_id).execute()
    except Exception:
        pass
    await update.message.reply_text("Search history cleared. Let's start fresh — what are you looking for?")


# ── Main message handler ──────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = str(update.effective_chat.id)
    user_text = update.message.text

    # Typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    reply = process_message(session_id=session_id, user_message=user_text, platform="telegram")

    # Check if a lead was just captured — if so, add broker contact buttons
    broker_buttons = _get_broker_buttons(session_id)
    if broker_buttons:
        await update.message.reply_text(reply, reply_markup=broker_buttons, parse_mode="Markdown")
    else:
        await update.message.reply_text(reply, parse_mode="Markdown")


def _get_broker_buttons(session_id: str) -> InlineKeyboardMarkup | None:
    """
    After lead capture, return inline buttons for calling broker via WhatsApp and Telegram.
    FREE — no Twilio, no telephony API, just deep links.
    """
    try:
        from database.supabase_client import get_client
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
            # WhatsApp deep link — opens WhatsApp with broker's number pre-filled (FREE)
            wa_url = f"https://wa.me/91{phone}?text=Hi%2C+I'm+interested+in+a+property+you+listed"
            buttons.append(InlineKeyboardButton("WhatsApp Broker", url=wa_url))

        if tg_id and tg_id.startswith("@"):
            # Telegram deep link (FREE)
            tg_url = f"https://t.me/{tg_id.lstrip('@')}"
            buttons.append(InlineKeyboardButton("Telegram Broker", url=tg_url))

        if not buttons:
            return None

        return InlineKeyboardMarkup([buttons])

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot @helper_panda_realesatet2bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
