import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from database import db
from client_manager import client_manager
from config import OWNER_ID, ADMIN_IDS

logger = logging.getLogger(__name__)

# Conversation states
SINGLE_SESSION, BULK_COUNT, BULK_SESSION = range(10, 13)

# Data keys for conversation
DATA_MODE = "add_mode"        # "single" or "bulk"
DATA_BULK_TOTAL = "bulk_total"
DATA_BULK_INDEX = "bulk_index"
DATA_BULK_LIST = "bulk_list"


async def add_account_menu(update: Update, context):
    """Show the add account menu with Single / Bulk options."""
    query = update.callback_query
    if query:
        await query.answer()
        message = query.message
    else:
        message = update.message

    keyboard = [
        [InlineKeyboardButton("📱 Single Add", callback_data="add_single")],
        [InlineKeyboardButton("📋 Bulk Add", callback_data="add_bulk")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ]
    text = "📥 *Add Account*\n\nChoose how you want to add accounts:"
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END


async def add_single_start(update: Update, context):
    """Start single account addition — ask for session string."""
    query = update.callback_query
    await query.answer()
    context.user_data[DATA_MODE] = "single"
    await query.edit_message_text(
        "📱 *Single Add*\n\nPlease send the Telethon session string for the account.\n\n"
        "💡 *How to get a session string:*\n"
        "`python3 -c \"from telethon.sessions import StringSession; "
        "from telethon import TelegramClient; "
        "c = TelegramClient(StringSession(), API_ID, API_HASH); "
        "c.start(); print(c.session.save()); c.disconnect()\"`",
        parse_mode="Markdown"
    )
    return SINGLE_SESSION


async def add_bulk_start(update: Update, context):
    """Start bulk addition — ask for count."""
    query = update.callback_query
    await query.answer()
    context.user_data[DATA_MODE] = "bulk"
    await query.edit_message_text(
        "📋 *Bulk Add*\n\nHow many accounts do you want to add?",
        parse_mode="Markdown"
    )
    return BULK_COUNT


async def handle_single_session(update: Update, context):
    """Receive and validate a single session string."""
    session_string = update.message.text.strip()
    status_msg = await update.message.reply_text("⏳ Validating session...")

    success, phone, error = await client_manager.validate_session(session_string)

    if not success:
        await status_msg.edit_text(f"❌ *Invalid Session*\n\nError: {error}\n\nPlease try again or send /cancel to abort.", parse_mode="Markdown")
        return SINGLE_SESSION

    # Check if already exists
    added = await db.add_account(phone, session_string)
    if not added:
        await status_msg.edit_text(
            f"⚠️ Account {phone} already exists in the database.\n\nSend another session or /cancel to abort.",
            parse_mode="Markdown"
        )
        return SINGLE_SESSION

    await status_msg.edit_text(f"✅ *Account Added Successfully!*\n\n📱 Phone: `{phone}`\n\nUse /start to go back to main menu.", parse_mode="Markdown")
    return ConversationHandler.END


async def handle_bulk_count(update: Update, context):
    """Receive bulk count and start asking for sessions."""
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Please send a valid number greater than 0.\n\nOr /cancel to abort.")
        return BULK_COUNT

    total = int(text)
    context.user_data[DATA_BULK_TOTAL] = total
    context.user_data[DATA_BULK_INDEX] = 0
    context.user_data[DATA_BULK_LIST] = []

    await update.message.reply_text(
        f"📋 *Bulk Add* — {total} accounts\n\n"
        f"Send session string **1/{total}**:",
        parse_mode="Markdown"
    )
    return BULK_SESSION


async def handle_bulk_session(update: Update, context):
    """Receive one session string during bulk addition."""
    session_string = update.message.text.strip()
    index = context.user_data.get(DATA_BULK_INDEX, 0) + 1
    total = context.user_data.get(DATA_BULK_TOTAL, 0)

    status_msg = await update.message.reply_text(f"⏳ Validating session {index}/{total}...")

    success, phone, error = await client_manager.validate_session(session_string)

    if not success:
        await status_msg.edit_text(
            f"❌ *Invalid Session* ({index}/{total})\nError: {error}\n\nPlease send a valid session string or /cancel to abort.",
            parse_mode="Markdown"
        )
        return BULK_SESSION

    # Add to database
    added = await db.add_account(phone, session_string)
    if not added:
        await status_msg.edit_text(
            f"⚠️ Account {phone} already exists. Skipping.\n\nSend session **{index}/{total}** (this one was skipped):",
            parse_mode="Markdown"
        )
        return BULK_SESSION

    # Store in user_data
    bulk_list = context.user_data.get(DATA_BULK_LIST, [])
    bulk_list.append(phone)
    context.user_data[DATA_BULK_LIST] = bulk_list
    context.user_data[DATA_BULK_INDEX] = index

    await status_msg.edit_text(f"✅ Account {phone} added! ({index}/{total})")

    if index >= total:
        # Done
        all_phones = context.user_data[DATA_BULK_LIST]
        phone_list = "\n".join(f"• `{p}`" for p in all_phones)
        await update.message.reply_text(
            f"🎉 *Bulk Add Complete!*\n\n{len(all_phones)} accounts added:\n{phone_list}\n\nUse /start for main menu.",
            parse_mode="Markdown"
        )
        # Clean up
        for key in [DATA_MODE, DATA_BULK_TOTAL, DATA_BULK_INDEX, DATA_BULK_LIST]:
            context.user_data.pop(key, None)
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            f"Send session string **{index + 1}/{total}**:",
            parse_mode="Markdown"
        )
        return BULK_SESSION


async def cancel(update: Update, context):
    """Cancel any ongoing conversation."""
    await update.message.reply_text("❌ Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def get_add_account_handler():
    """Return the ConversationHandler for adding accounts."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_single_start, pattern="^add_single$"),
            CallbackQueryHandler(add_bulk_start, pattern="^add_bulk$"),
        ],
        states={
            SINGLE_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_single_session),
            ],
            BULK_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bulk_count),
            ],
            BULK_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bulk_session),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ]
    
    )
