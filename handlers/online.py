import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from database import db
from client_manager import client_manager

logger = logging.getLogger(__name__)


async def all_accounts_online(update: Update, context):
    """
    Bring all accounts online AND re-apply their mode conditions strictly.
    Each account's profile is updated to match its mode.
    """
    query = update.callback_query
    await query.answer()

    status_msg = await query.edit_message_text(
        "🌐 *Enforcing mode conditions for all accounts...*\n"
        "Each account profile will be updated to match its assigned mode.\n"
        "Please wait...",
        parse_mode="Markdown"
    )

    results = await client_manager.enforce_modes_for_all_accounts()

    text = (
        f"🌐 *All Accounts — Modes Enforced*\n\n"
        f"📊 Results:\n"
        f"• Mode 1 (Permanent Online): ✅ {results['mode1']}\n"
        f"• Mode 2 (Hidden Last Seen): ✅ {results['mode2']}\n"
        f"• Mode 3 (Offline after 2min): ✅ {results['mode3']}\n"
        f"• ❌ Failed: {results['failed']}\n\n"
        f"All profiles updated to match their mode condition."
    )

    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]
    await status_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
