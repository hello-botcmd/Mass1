import logging
from telegram import Update
from telegram.ext import CommandHandler
from database import db

logger = logging.getLogger(__name__)

# Import stop functions from other handlers
from handlers.join import stop_join
from handlers.reactions import stop_reactions
from handlers.views import stop_views


async def stop_command(update: Update, context):
    """Stop any ongoing process."""
    user_id = update.effective_user.id
    from config import OWNER_ID, ADMIN_IDS

    if user_id != OWNER_ID and user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    # Stop all processes
    await stop_join()
    await stop_reactions()
    await stop_views()
    await db.stop_all_processes()
    await db.reset_all_busy()

    await update.message.reply_text(
        "⏹ *All ongoing processes have been stopped.*\n\n"
        "• Join process: stopped\n"
        "• Reaction process: stopped\n"
        "• View boost: stopped\n"
        "• All accounts marked as available.",
        parse_mode="Markdown"
    )
