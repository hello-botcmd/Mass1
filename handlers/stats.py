import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from database import db
from client_manager import client_manager

logger = logging.getLogger(__name__)


async def show_stats(update: Update, context):
    """Show total and active account counts."""
    query = update.callback_query
    await query.answer()

    total = await db.get_accounts_count()
    active = await db.get_active_accounts_count()
    online_count = len(client_manager.clients)
    online_ping_count = len(client_manager.online_tasks)

    # Get mode distribution
    all_accounts = await db.get_all_accounts()
    mode1 = sum(1 for a in all_accounts if a.get("mode") == 1)
    mode2 = sum(1 for a in all_accounts if a.get("mode") == 2)
    mode3 = sum(1 for a in all_accounts if a.get("mode") == 3)
    idle_count = total - mode1 - mode2 - mode3

    text = (
        f"📊 *Account Statistics*\n\n"
        f"• 📱 Total Accounts: `{total}`\n"
        f"• 🟢 Online (ping active): `{online_ping_count}`\n"
        f"• 🔵 Connected clients: `{online_count}`\n"
        f"• ✅ Active (DB flag): `{active}`\n\n"
        f"*Mode Distribution:*\n"
        f"• Mode 1 (Permanent Online): `{mode1}`\n"
        f"• Mode 2 (Hidden Last Seen): `{mode2}`\n"
        f"• Mode 3 (Offline after 2min): `{mode3}`\n"
        f"• Unassigned / Idle: `{idle_count}`"
    )

    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
