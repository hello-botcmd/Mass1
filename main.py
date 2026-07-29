import asyncio
import logging
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
from telegram.constants import ParseMode

from config import BOT_TOKEN, OWNER_ID, ADMIN_IDS
from database import db
from client_manager import client_manager

from handlers.add_account import get_add_account_handler, add_account_menu
from handlers.join import get_join_handler
from handlers.reactions import get_reactions_handler
from handlers.views import get_views_handler
from handlers.online import all_accounts_online
from handlers.stats import show_stats
from handlers.remove import remove_from_chat
from handlers.stop import stop_command

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def is_authorized(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in ADMIN_IDS


async def start(update: Update, context):
    """Main menu — show all available actions."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    keyboard = [
        [InlineKeyboardButton("📥 Add Account", callback_data="add_account")],
        [InlineKeyboardButton("🔗 Join Channel/Group", callback_data="join")],
        [InlineKeyboardButton("📊 Total Accounts", callback_data="stats")],
        [InlineKeyboardButton("❤️ Reaction", callback_data="reactions")],
        [InlineKeyboardButton("👁 Boosts Views", callback_data="views")],
        [InlineKeyboardButton("🌐 All Accounts Online", callback_data="online")],
    ]

    text = (
        "🤖 *Telegram Account Manager*\n\n"
        "Welcome! Select an action below:\n\n"
        f"• Use `/stop` to halt any ongoing process\n"
        f"• Use `/remove chat_id` to remove accounts from a chat\n"
        f"• Use `/cancel` during any setup to abort"
    )

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def main_menu_callback(update: Update, context):
    """Route callbacks to appropriate handlers."""
    query = update.callback_query
    await query.answer()

    callback_map = {
        "add_account": add_account_menu,
        "join": None,  # Handled by ConversationHandler
        "stats": show_stats,
        "reactions": None,  # Handled by ConversationHandler
        "views": None,  # Handled by ConversationHandler
        "online": all_accounts_online,
        "main_menu": start,
    }

    handler = callback_map.get(query.data)
    if handler:
        if query.data == "add_account":
            await handler(update, context)
        elif query.data == "main_menu":
            await handler(update, context)
        elif query.data == "stats":
            await handler(update, context)
        elif query.data == "online":
            await handler(update, context)


async def post_init(application):
    """Initialize database connection."""
    logger.info("Bot started. Database connected.")


async def post_shutdown(application):
    """Clean up resources."""
    await client_manager.disconnect_all()
    await db.cleanup()
    logger.info("Bot shut down. Resources cleaned.")


def main():
    # Build application
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ─── Basic commands ───
    application.add_handler(CommandHandler("start", start, filters=filters.User(user_id=[OWNER_ID] + ADMIN_IDS)))
    application.add_handler(CommandHandler("stop", stop_command, filters=filters.User(user_id=[OWNER_ID] + ADMIN_IDS)))
    application.add_handler(CommandHandler("remove", remove_from_chat, filters=filters.User(user_id=[OWNER_ID] + ADMIN_IDS)))

    # ─── Conversation handlers ───
    application.add_handler(get_add_account_handler())
    application.add_handler(get_join_handler())
    application.add_handler(get_reactions_handler())
    application.add_handler(get_views_handler())

    # ─── Callback router (for main menu and non-conversation callbacks) ───
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^(add_account|stats|online|main_menu)$"))

    # ─── Error handler ───
    async def error_handler(update: Update, context):
        logger.error(f"Update {update} caused error {context.error}")
        try:
            if update and update.effective_chat:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"❌ An error occurred: {str(context.error)[:200]}"
                )
        except Exception:
            pass

    application.add_error_handler(error_handler)

    logger.info("Starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
